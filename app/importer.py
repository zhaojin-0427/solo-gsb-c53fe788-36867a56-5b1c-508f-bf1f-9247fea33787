"""Atomic batch import, threading and thread-merge logic.

All work for one (mailbox, batch) happens in a single transaction guarded by
a mailbox-scoped advisory lock, so concurrent imports of the same mailbox are
serialized while different mailboxes proceed independently.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from .eml import ParsedMessage, ParseError, parse_eml


class BatchRejected(Exception):
    """Whole-batch validation failure; nothing is committed."""

    def __init__(self, code: str, details: list[dict]):
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass
class Item:
    index: int
    raw: bytes
    parsed: ParsedMessage


@dataclass
class _Row:
    msg_id: str
    is_placeholder: bool
    raw_sha256: bytes | None
    parent_id: str | None
    parent_inferred: bool


@dataclass
class _Plan:
    items: dict[str, Item]                      # msg_id -> unique item
    # nodes present in the database when the batch started
    preexisting: set[str] = field(default_factory=set)
    # nodes carrying an edge established/corrected by this batch
    events: dict[str, str | None] = field(default_factory=dict)
    inserts_real: list[Item] = field(default_factory=list)
    fills: list[tuple[str, Item]] = field(default_factory=list)
    inserts_placeholder: list[tuple[str, str | None]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Python-side validation
# --------------------------------------------------------------------------

def parse_batch(raw_items: list[bytes]) -> dict[str, Item]:
    """Parse every EML and enforce per-message constraints."""
    if not raw_items:
        raise BatchRejected("empty_batch", [])

    items: dict[str, Item] = {}
    errors: list[dict] = []

    for index, raw in enumerate(raw_items):
        try:
            parsed = parse_eml(raw)
        except ParseError as exc:
            errors.append({"index": index, "code": "malformed_eml",
                           "message": str(exc)})
            continue

        if parsed.parent_id == parsed.msg_id or parsed.msg_id in parsed.references:
            errors.append({"index": index, "code": "self_reference",
                           "message_id": parsed.msg_id,
                           "message": "message references its own Message-ID"})
            continue

        previous = items.get(parsed.msg_id)
        if previous is not None:
            if hashlib.sha256(previous.raw).digest() != hashlib.sha256(raw).digest():
                errors.append({"index": index, "code": "conflicting_content",
                               "message_id": parsed.msg_id,
                               "message": "same Message-ID with different raw bytes"})
            # identical duplicate inside the batch: silently deduplicated
            continue

        items[parsed.msg_id] = Item(index=index, raw=raw, parsed=parsed)

    if errors:
        raise BatchRejected("rejected", errors)
    return items


def _fingerprint(mailbox_id: int, items: dict[str, Item]) -> bytes:
    h = hashlib.sha256()
    h.update(mailbox_id.to_bytes(8, "big"))
    for msg_id in sorted(items):
        raw = items[msg_id].raw
        h.update(msg_id.encode("utf-8"))
        h.update(b":")
        h.update(hashlib.sha256(raw).digest())
        h.update(b";")
    return h.digest()


# --------------------------------------------------------------------------
# Transaction
# --------------------------------------------------------------------------

def import_batch(conn, mailbox_name: str, raw_items: list[bytes]) -> dict:
    items = parse_batch(raw_items)

    # ensure mailbox
    conn.execute(
        "INSERT INTO mailboxes(name) VALUES(%s) ON CONFLICT DO NOTHING",
        (mailbox_name,),
    )
    row = conn.execute(
        "SELECT id FROM mailboxes WHERE name=%s", (mailbox_name,)
    ).fetchone()
    mailbox_id = row[0]

    # serialize writers of the same mailbox
    conn.execute("SELECT pg_advisory_xact_lock(%s, 0)", (mailbox_id,))

    fp = _fingerprint(mailbox_id, items)
    dup = conn.execute(
        "SELECT rev, merged, item_count FROM revisions "
        "WHERE mailbox_id=%s AND batch_fp=%s",
        (mailbox_id, psycopg.Binary(fp)),
    ).fetchone()
    if dup is not None:
        return {
            "mailbox": mailbox_name,
            "revision": dup[0],
            "effective": False,
            "duplicate": True,
            "merged": dup[1] or {},
            "imported": dup[2],
        }

    referenced = set(items)
    inferred_preds: dict[str, str] = {}
    for it in items.values():
        p = it.parsed
        referenced.update(p.references)
        if p.parent_id:
            referenced.add(p.parent_id)
        # reference order supplies ancestry for not-yet-seen intermediates
        chain = list(p.references) + [p.msg_id]
        for child, pred in zip(chain[1:], chain[:-1]):
            inferred_preds.setdefault(child, pred)

    rows = _load_rows(conn, mailbox_id, referenced)

    plan = _build_plan(items, rows, inferred_preds)

    # cycles against the stored forest plus every edge set by this batch
    for v, u in plan.events.items():
        if _creates_cycle(conn, mailbox_id, v, u, plan.events):
            raise BatchRejected("rejected", [{
                "index": items[v].index if v in items else None,
                "code": "cycle",
                "message_id": v,
                "message": "batch would create a parent cycle",
            }])

    # Old/new thread-number mapping. A merge happens when a pre-existing
    # subtree root changes because an edge of this batch moved it (typical
    # case: a late parent attaches an existing placeholder subtree).
    merged: dict[str, str] = {}
    for v in plan.preexisting:
        if v not in plan.events:
            continue
        old_root = _db_root(conn, mailbox_id, v)
        new_root = _new_root(conn, mailbox_id, v, plan.events)
        if old_root != new_root and old_root not in merged:
            # keep the first mapping per old root; moved siblings belonging
            # to one subtree resolve to the same new root
            merged[old_root] = new_root

    latest = conn.execute(
        "SELECT COALESCE(MAX(rev), 0) FROM revisions WHERE mailbox_id=%s",
        (mailbox_id,),
    ).fetchone()[0]

    if not plan.events and not plan.fills:
        # every message already stored byte-identical: no effective change
        return {
            "mailbox": mailbox_name,
            "revision": latest,
            "effective": False,
            "duplicate": False,
            "merged": {},
            "imported": len(items),
        }

    rev = latest + 1
    _persist(conn, mailbox_id, rev, plan)

    conn.execute(
        "INSERT INTO revisions(mailbox_id, rev, batch_fp, item_count, merged) "
        "VALUES(%s,%s,%s,%s,%s)",
        (mailbox_id, rev, psycopg.Binary(fp), len(items),
         Jsonb(merged)),
    )

    return {
        "mailbox": mailbox_name,
        "revision": rev,
        "effective": True,
        "duplicate": False,
        "merged": merged,
        "imported": len(items),
        "new_messages": len(plan.inserts_real),
        "filled_placeholders": len(plan.fills),
        "placeholders_created": len(plan.inserts_placeholder),
    }


def _load_rows(conn, mailbox_id: int, ids: set[str]) -> dict[str, _Row]:
    if not ids:
        return {}
    res = conn.execute(
        "SELECT msg_id, is_placeholder, raw_sha256, parent_id, parent_inferred "
        "FROM nodes WHERE mailbox_id=%s AND msg_id = ANY(%s)",
        (mailbox_id, list(ids)),
    )
    return {
        r[0]: _Row(msg_id=r[0], is_placeholder=r[1], raw_sha256=r[2],
                   parent_id=r[3], parent_inferred=r[4])
        for r in res.fetchall()
    }


def _build_plan(
    items: dict[str, Item],
    rows: dict[str, _Row],
    inferred_preds: dict[str, str],
) -> _Plan:
    plan = _Plan(items=items, preexisting=set(rows))

    # check content conflicts against stored messages
    for msg_id, it in items.items():
        row = rows.get(msg_id)
        digest = hashlib.sha256(it.raw).digest()
        if row is not None and not row.is_placeholder:
            if row.raw_sha256 != digest:
                # can't pin a single batch index uniquely; report the message
                raise BatchRejected("rejected", [{
                    "index": it.index,
                    "code": "conflicting_content",
                    "message_id": msg_id,
                    "message": "stored Message-ID re-imported with different bytes",
                }])

    # real messages: desired parent = In-Reply-To, else last reference.
    # stored placeholders only ever carry guessed ancestry (or no ancestry),
    # so genuine evidence always wins -- moving the subtree is the merge.
    for msg_id, it in items.items():
        desired = it.parsed.parent_id
        row = rows.get(msg_id)
        if row is None:
            plan.inserts_real.append(it)
            plan.events[msg_id] = desired
        elif row.is_placeholder:
            if desired != row.parent_id:
                plan.fills.append((msg_id, it))
                plan.events[msg_id] = desired  # may be None: genuine evidence says root
            else:
                plan.fills.append((msg_id, it))
        # existing real identical message: nothing to do

    # placeholders for referenced ids that are still unknown
    referenced_all = set(inferred_preds) | {
        p for it in items.values() for p in (
            it.parsed.references
            + ((it.parsed.parent_id,) if it.parsed.parent_id else ())
        )
    }
    for ref in sorted(referenced_all):
        if ref in items or ref in rows:
            continue
        pred = inferred_preds.get(ref)  # may be None: ancestry unknown
        plan.inserts_placeholder.append((ref, pred))
        plan.events[ref] = pred  # recorded even when NULL (node creation)

    return plan


_ANCESTORS_SQL = """
    WITH RECURSIVE chain(node_id, parent_id, depth) AS (
        SELECT msg_id, parent_id, 0 FROM nodes
        WHERE mailbox_id = %s AND msg_id = %s
        UNION ALL
        SELECT n.msg_id, n.parent_id, c.depth + 1
        FROM nodes n JOIN chain c ON n.msg_id = c.parent_id
        WHERE n.mailbox_id = %s AND c.depth < 10000000
    )
    SELECT node_id, parent_id FROM chain ORDER BY depth
"""


def _db_chain(conn, mailbox_id: int, start: str) -> list[tuple[str, str | None]]:
    """Full ancestor chain of ``start`` stored in the database (start first)."""
    return conn.execute(_ANCESTORS_SQL, (mailbox_id, start, mailbox_id)).fetchall()


def _walk_with_override(
    conn, mailbox_id: int, start: str | None,
    overrides: dict[str, str | None],
    stop: str | None = None,
) -> tuple[str, bool]:
    """Walk parents from ``start`` using batch overrides atop stored edges.

    Returns ``(end_node, hit_stop)``. Database chains are fetched lazily with a
    single recursive query per chain; we hop back to local walking whenever a
    chain reaches an overridden edge, which keeps the common case
    (supplementing a leaf) SQL-free.
    """
    cur = start
    seen: set[str] = set()
    while cur is not None:
        if stop is not None and cur == stop:
            return cur, True
        if cur in seen:  # stored graph is a forest; defensive guard
            return cur, False
        seen.add(cur)
        if cur in overrides:
            nxt = overrides[cur]
            if nxt is None:
                return cur, False  # genuine edge says this node is the root
            cur = nxt
            continue
        # not overridden: pull its whole stored ancestor chain at once
        chain = _db_chain(conn, mailbox_id, cur)
        if not chain:
            # node introduced by this batch with no DB row: parent edge is
            # necessarily an override already handled above, so it is a root
            return cur, False
        for node_id, parent_id in chain:
            if stop is not None and node_id == stop:
                return node_id, True
            if node_id in overrides:
                cur = overrides[node_id]
                break
        else:
            # chain ended at a stored root
            return chain[-1][0], False
    return "", False  # reached NULL parent from an override


def _creates_cycle(
    conn, mailbox_id: int, child: str, parent: str | None,
    overrides: dict[str, str | None],
) -> bool:
    """True if adding edge child -> parent makes ``child`` its own ancestor."""
    if parent is None:
        return False
    _, hit = _walk_with_override(conn, mailbox_id, parent, overrides, stop=child)
    return hit


def _db_root(conn, mailbox_id: int, node: str) -> str:
    chain = _db_chain(conn, mailbox_id, node)
    return chain[-1][0] if chain else node


def _new_root(
    conn, mailbox_id: int, node: str, overrides: dict[str, str | None]
) -> str:
    root, _ = _walk_with_override(conn, mailbox_id, node, overrides)
    return root


def _persist(conn, mailbox_id: int, rev: int, plan: _Plan) -> None:
    # new real messages
    for it in plan.inserts_real:
        conn.execute(
            "INSERT INTO nodes(mailbox_id, msg_id, is_placeholder, "
            "origin_placeholder, raw_bytes, raw_sha256, parent_id, "
            "parent_inferred, fill_rev, created_rev) "
            "VALUES(%s,%s,FALSE,FALSE,%s,%s,%s,FALSE,NULL,%s)",
            (mailbox_id, it.parsed.msg_id, psycopg.Binary(it.raw),
             hashlib.sha256(it.raw).digest(), it.parsed.parent_id, rev),
        )

    # placeholders filled by this batch (content only; a differing genuine
    # parent is applied through plan.events, exactly like any moved edge)
    for msg_id, it in plan.fills:
        conn.execute(
            "UPDATE nodes SET is_placeholder=FALSE, raw_bytes=%s, "
            "raw_sha256=%s, fill_rev=%s "
            "WHERE mailbox_id=%s AND msg_id=%s",
            (psycopg.Binary(it.raw), hashlib.sha256(it.raw).digest(),
             rev, mailbox_id, msg_id),
        )

    # edges corrected by this batch (fill whose guessed parent was wrong,
    # including a genuine NULL parent that detaches a subtree)
    for node, parent in plan.events.items():
        conn.execute(
            "UPDATE nodes SET parent_id=%s, parent_inferred=FALSE "
            "WHERE mailbox_id=%s AND msg_id=%s",
            (parent, mailbox_id, node),
        )

    # newly created placeholders
    for ref, pred in plan.inserts_placeholder:
        conn.execute(
            "INSERT INTO nodes(mailbox_id, msg_id, is_placeholder, "
            "origin_placeholder, raw_bytes, raw_sha256, parent_id, "
            "parent_inferred, fill_rev, created_rev) "
            "VALUES(%s,%s,TRUE,TRUE,NULL,NULL,%s,%s,NULL,%s)",
            (mailbox_id, ref, pred, pred is not None, rev),
        )

    # append-only link history
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO link_events(mailbox_id, rev, node_id, parent_id) "
            "VALUES(%s,%s,%s,%s)",
            [(mailbox_id, rev, node, parent)
             for node, parent in plan.events.items()],
        )


# --------------------------------------------------------------------------
# Thread reconstruction
# --------------------------------------------------------------------------

def get_threads(conn, mailbox_name: str, revision: int | None) -> dict:
    row = conn.execute(
        "SELECT id FROM mailboxes WHERE name=%s", (mailbox_name,)
    ).fetchone()
    if row is None:
        raise KeyError(mailbox_name)
    mailbox_id = row[0]

    latest = conn.execute(
        "SELECT COALESCE(MAX(rev), 0) FROM revisions WHERE mailbox_id=%s",
        (mailbox_id,),
    ).fetchone()[0]
    if revision is None:
        revision = latest
    if revision < 1 or revision > latest:
        raise LookupError(revision)

    # Parent pointer and missing status of every node existing at `revision`.
    # The latest revision is answered straight from the live nodes table;
    # older revisions replay link_events (DISTINCT ON with a supporting index
    # avoids sorting the whole event history).
    if revision == latest:
        res = conn.execute(
            """
            SELECT msg_id, parent_id, is_placeholder
            FROM nodes
            WHERE mailbox_id = %s AND created_rev <= %s
            """,
            (mailbox_id, revision),
        )
    else:
        res = conn.execute(
            """
            SELECT DISTINCT ON (le.node_id)
                   le.node_id, le.parent_id,
                   (n.origin_placeholder = TRUE AND
                        (n.fill_rev IS NULL OR n.fill_rev > %s)) AS was_missing
            FROM link_events le
            JOIN nodes n
              ON n.mailbox_id = %s AND n.msg_id = le.node_id
            WHERE le.mailbox_id = %s AND le.rev <= %s
              AND n.created_rev <= %s
            ORDER BY le.node_id, le.rev DESC
            """,
            (revision, mailbox_id, mailbox_id, revision, revision),
        )

    parents: dict[str, str | None] = {}
    missing: set[str] = set()
    placeholder_flag: dict[str, bool] = {}
    for node_id, parent_id, was_missing in res.fetchall():
        parents[node_id] = parent_id
        placeholder_flag[node_id] = was_missing
        if was_missing:
            missing.add(node_id)

    children: dict[str, list[str]] = {n: [] for n in parents}
    roots: list[str] = []
    for node, parent in parents.items():
        if parent is None or parent not in parents:
            roots.append(node)
        else:
            children[parent].append(node)
    for kids in children.values():
        kids.sort()

    # Build trees iteratively; a single thread may chain tens of thousands of
    # messages deep and would otherwise blow the recursion limit.
    def materialize(root: str) -> dict:
        root_entry: dict = {}
        # frames: (entry, next_child_index)
        frame_stack: list[tuple[dict, int]] = []

        def new_entry(node: str) -> dict:
            return {
                "message_id": node,
                "missing": placeholder_flag.get(node, False),
                "children": [],
            }

        root_entry = new_entry(root)
        frame_stack.append((root_entry, 0))
        while frame_stack:
            entry, idx = frame_stack[-1]
            kids = children[entry["message_id"]]
            if idx >= len(kids):
                frame_stack.pop()
                continue
            frame_stack[-1] = (entry, idx + 1)
            child_entry = new_entry(kids[idx])
            entry["children"].append(child_entry)
            frame_stack.append((child_entry, 0))
        return root_entry

    threads = [
        {"thread_id": r, "root_missing": r in missing, "tree": materialize(r)}
        for r in sorted(roots)
    ]
    return {
        "mailbox": mailbox_name,
        "revision": revision,
        "latest_revision": latest,
        "threads": threads,
        "missing": sorted(missing),
    }

"""Concurrency and edge-case tests below the HTTP layer."""
from __future__ import annotations

import threading

from app import db as dbmod
from app.importer import import_batch
from tests.eml_factory import eml


def _conn():
    return dbmod.get_pool().connection()


def test_concurrent_same_mailbox_is_serialized_and_consistent(client):
    # N threads each import a disjoint batch replying to the previous id of
    # the same mailbox; no deadlock, and the final tree is a single chain
    n = 20
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            with _conn() as conn:
                parent = f"m{i - 1}@x" if i > 0 else None
                msg = eml(f"m{i}@x", in_reply_to=parent) if parent else eml(f"m{i}@x")
                import_batch(conn, "race", [msg])
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with _conn() as conn:
        from app.importer import get_threads

        t = get_threads(conn, "race", None)
        assert len(t["threads"]) == 1
        # walk the chain
        node = t["threads"][0]["tree"]
        seen = [node["message_id"]]
        while node["children"]:
            assert len(node["children"]) == 1
            node = node["children"][0]
            seen.append(node["message_id"])
        assert set(seen) == {f"m{i}@x" for i in range(n)}


def test_different_mailboxes_do_not_block_each_other(client):
    done = threading.Event()

    def slow_mailbox():
        with _conn() as conn:
            import_batch(conn, "slow", [eml("a@x")])
            conn.commit()
            done.set()

    t = threading.Thread(target=slow_mailbox)
    t.start()
    t.join(timeout=10)
    with _conn() as conn:
        r = import_batch(conn, "fast", [eml("z@x")])
        conn.commit()
    assert r["effective"] is True
    assert done.is_set()


def test_partial_reimport_no_revision_change(client):
    with _conn() as conn:
        r1 = import_batch(conn, "mb", [eml("a@x"), eml("b@x", in_reply_to="a@x")])
        conn.commit()
        assert r1["effective"] is True

        r2 = import_batch(conn, "mb", [eml("a@x")])  # subset, same bytes
        conn.commit()
        assert r2["effective"] is False
        assert r2["duplicate"] is False
        assert r2["revision"] == r1["revision"]


def test_empty_payload_rejected(client):
    r = client.post(
        "/imports", data={"mailbox": "mb"},
        files=[("messages", ("empty.eml", b"", "message/rfc822"))],
    )
    assert r.status_code == 422


def test_blank_mailbox_rejected(client):
    r = client.post(
        "/imports", data={"mailbox": "   "},
        files=[("messages", ("a.eml", eml("a@x"), "message/rfc822"))],
    )
    assert r.status_code == 422


def test_multiple_errors_reported_together(client):
    # one message missing ID, one self-referencing: both details returned
    bad1 = b"Subject: no id\r\n\r\nbody"
    bad2 = eml("s@x", in_reply_to="s@x")
    r = client.post(
        "/imports", data={"mailbox": "mb"},
        files=[("messages", ("a.eml", bad1, "message/rfc822")),
               ("messages", ("b.eml", bad2, "message/rfc822"))],
    )
    assert r.status_code == 422
    codes = {d["code"] for d in r.json()["error"]["details"]}
    assert codes == {"malformed_eml", "self_reference"}

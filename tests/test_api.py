"""Functional tests for /imports and /threads."""
from __future__ import annotations

from tests.eml_factory import eml


def import_(client, mailbox, *messages):
    files = [("messages", (f"m{i}.eml", m, "message/rfc822"))
             for i, m in enumerate(messages)]
    return client.post(
        "/imports", data={"mailbox": mailbox}, files=files
    )


def roots(payload: dict) -> dict[str, dict]:
    return {t["thread_id"]: t for t in payload["threads"]}


def tree_ids(node: dict) -> list:
    return [node["message_id"]] + [
        c for ch in node["children"] for c in tree_ids(ch)
    ]


# ---------------------------------------------------------------- basics ----

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_single_message_creates_revision_and_thread(client):
    r = import_(client, "mb", eml("root@x"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["effective"] is True
    assert body["new_messages"] == 1

    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert list(roots(t)) == ["root@x"]
    assert t["missing"] == []


def test_in_reply_to_builds_child(client):
    r = import_(client, "mb", eml("root@x"), eml("c@x", in_reply_to="root@x"))
    assert r.status_code == 200
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    root = roots(t)["root@x"]
    assert [c["message_id"] for c in root["tree"]["children"]] == ["c@x"]
    assert root["tree"]["children"][0]["children"] == []


def test_references_last_id_is_parent(client):
    r = import_(client, "mb",
                eml("a@x"), eml("b@x", references=["a@x"]),
                eml("c@x", references=["a@x", "b@x"]))
    assert r.status_code == 200
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    root = roots(t)["a@x"]
    b = root["tree"]["children"][0]
    assert b["message_id"] == "b@x"
    assert [d["message_id"] for d in b["children"]] == ["c@x"]


def test_in_reply_to_takes_precedence_over_references(client):
    r = import_(client, "mb",
                eml("a@x"), eml("b@x"),
                eml("c@x", in_reply_to="a@x", references=["a@x", "b@x"]))
    assert r.status_code == 200
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    root = roots(t)["a@x"]
    assert [c["message_id"] for c in root["tree"]["children"]] == ["c@x"]
    assert roots(t)["b@x"]["tree"]["children"] == []


# ------------------------------------------------------------ placeholders --

def test_missing_parent_is_placeholder_and_reported(client):
    r = import_(client, "mb", eml("child@x", in_reply_to="ghost@x"))
    assert r.status_code == 200
    assert r.json()["placeholders_created"] == 1

    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert t["missing"] == ["ghost@x"]
    root = roots(t)["ghost@x"]
    assert root["root_missing"] is True
    assert root["tree"]["missing"] is True
    assert [c["message_id"] for c in root["tree"]["children"]] == ["child@x"]


def test_late_parent_merges_threads_and_reports_mapping(client):
    # child arrives first, its missing parent is a placeholder root
    import_(client, "mb", eml("child@x", in_reply_to="ghost@x"))
    # the late parent arrives and itself points at the true root
    merge = import_(client, "mb",
                    eml("ghost@x", in_reply_to="real-root@x"))
    assert merge.status_code == 200
    body = merge.json()
    assert body["effective"] is True
    # old thread number ghost@x merged under real-root@x
    assert body["merged"] == {"ghost@x": "real-root@x"}

    # and the root itself finally fills the remaining placeholder
    root = import_(client, "mb", eml("real-root@x"))
    assert root.json()["revision"] == body["revision"] + 1

    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert set(roots(t)) == {"real-root@x"}
    assert t["missing"] == []
    r = roots(t)["real-root@x"]["tree"]
    assert r["message_id"] == "real-root@x"
    g = r["children"][0]
    assert g["message_id"] == "ghost@x"
    assert g["children"][0]["message_id"] == "child@x"

    # re-posting the exact same bytes returns the original revision
    again = import_(client, "mb",
                    eml("ghost@x", in_reply_to="real-root@x"))
    assert again.json()["revision"] == body["revision"]
    assert again.json()["duplicate"] is True
    assert again.json()["merged"] == {"ghost@x": "real-root@x"}


def test_inferred_chain_merges_when_late_parent_arrives(client):
    # child references the whole chain; intermediates are placeholders with
    # inferred parents
    r = import_(client, "mb",
                eml("leaf@x", references=["r@x", "mid@x"]))
    assert r.status_code == 200
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert set(t["missing"]) == {"mid@x", "r@x"}

    import_(client, "mb", eml("mid@x", in_reply_to="r@x"))
    r = import_(client, "mb", eml("r@x"))
    assert r.json()["merged"] == {}
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    root = roots(t)["r@x"]["tree"]
    assert root["children"][0]["message_id"] == "mid@x"
    assert root["children"][0]["children"][0]["message_id"] == "leaf@x"
    assert t["missing"] == []


def test_fill_with_unexpected_real_parent_corrects_inferred_edge(client):
    # inferred: mid@x -> p@x ; real mid@x says its parent is q@x instead
    import_(client, "mb", eml("leaf@x", references=["p@x", "mid@x"]))
    r = import_(client, "mb", eml("mid@x", in_reply_to="q@x"))
    assert r.status_code == 200
    assert r.json()["merged"] == {"p@x": "q@x"}
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert set(roots(t)) == {"q@x", "p@x"}  # p becomes empty root
    q = roots(t)["q@x"]["tree"]
    assert q["children"][0]["message_id"] == "mid@x"
    assert q["children"][0]["children"][0]["message_id"] == "leaf@x"


# -------------------------------------------------------------- revisions --

def test_get_specific_revision_shows_old_tree(client):
    import_(client, "mb", eml("child@x", in_reply_to="ghost@x"))  # rev 1
    import_(client, "mb", eml("ghost@x"))                          # rev 2 merge

    t1 = client.get("/threads", params={"mailbox": "mb", "revision": 1}).json()
    assert set(roots(t1)) == {"ghost@x"}
    assert t1["missing"] == ["ghost@x"]

    t2 = client.get("/threads", params={"mailbox": "mb", "revision": 2}).json()
    assert set(roots(t2)) == {"ghost@x"}
    assert t2["missing"] == []


def test_history_is_not_rewritten_by_later_merges(client):
    # two independent threads: a root, and a placeholder root ph with child b
    import_(client, "mb", eml("a@x"))
    import_(client, "mb", eml("b@x", in_reply_to="ph@x"))
    before = client.get("/threads", params={"mailbox": "mb", "revision": 2}).json()
    assert set(roots(before)) == {"a@x", "ph@x"}

    # late real ph@x attaches its subtree under a@x: one merge
    m = import_(client, "mb", eml("ph@x", in_reply_to="a@x"))
    assert m.json()["merged"] == {"ph@x": "a@x"}

    after = client.get("/threads", params={"mailbox": "mb", "revision": 2}).json()
    assert after["revision"] == before["revision"] == 2
    assert after["threads"] == before["threads"]  # history never rewritten
    assert after["missing"] == before["missing"]
    latest = client.get("/threads", params={"mailbox": "mb"}).json()
    assert set(roots(latest)) == {"a@x"}


def test_unknown_revision_404(client):
    import_(client, "mb", eml("a@x"))
    r = client.get("/threads", params={"mailbox": "mb", "revision": 99})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_revision"


def test_unknown_mailbox_404(client):
    r = client.get("/threads", params={"mailbox": "nope"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_mailbox"


# -------------------------------------------------------------- idempotency -

def test_same_id_same_bytes_returns_original_revision(client):
    first = import_(client, "mb", eml("a@x"), eml("b@x", in_reply_to="a@x"))
    rev = first.json()["revision"]

    again = import_(client, "mb", eml("b@x", in_reply_to="a@x"), eml("a@x"))
    body = again.json()
    assert again.status_code == 200
    assert body["effective"] is False
    assert body["duplicate"] is True
    assert body["revision"] == rev


def test_reorder_batch_gives_same_tree(client):
    msgs = [
        eml("a@x"),
        eml("b@x", in_reply_to="a@x"),
        eml("c@x", in_reply_to="b@x"),
        eml("d@x", references=["a@x", "b@x", "c@x"]),
    ]
    import_(client, "one", *msgs)
    import_(client, "two", *reversed(msgs))
    t1 = client.get("/threads", params={"mailbox": "one"}).json()
    t2 = client.get("/threads", params={"mailbox": "two"}).json()
    assert t1["threads"] == t2["threads"]


def test_mailboxes_are_independent(client):
    import_(client, "mb1", eml("shared@x", in_reply_to="p1@x"))
    import_(client, "mb2", eml("shared@x", in_reply_to="p2@x"))
    t1 = client.get("/threads", params={"mailbox": "mb1"}).json()
    t2 = client.get("/threads", params={"mailbox": "mb2"}).json()
    assert [c["message_id"] for c in roots(t1)["p1@x"]["tree"]["children"]] == ["shared@x"]
    assert [c["message_id"] for c in roots(t2)["p2@x"]["tree"]["children"]] == ["shared@x"]


# ------------------------------------------------------------- validation --

def test_missing_message_id_rejects_whole_batch(client):
    r = import_(client, "mb", eml(None), eml("ok@x"))
    assert r.status_code == 422
    codes = {d["code"] for d in r.json()["error"]["details"]}
    assert "malformed_eml" in codes
    # revision did not advance
    t = client.get("/threads", params={"mailbox": "mb"})
    assert t.status_code == 404


def test_different_bytes_same_id_rejects_batch(client):
    r = import_(client, "mb", eml("a@x", body="one"), eml("a@x", body="two"))
    assert r.status_code == 422
    assert any(d["code"] == "conflicting_content"
               for d in r.json()["error"]["details"])


def test_different_bytes_against_stored_rejects_batch(client):
    import_(client, "mb", eml("a@x", body="one"))
    r = import_(client, "mb", eml("a@x", body="two"))
    assert r.status_code == 422
    assert any(d["code"] == "conflicting_content"
               for d in r.json()["error"]["details"])
    # rev 1 still the latest; nothing written by the failed batch
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert t["revision"] == 1


def test_self_reference_rejected(client):
    r = import_(client, "mb", eml("a@x", in_reply_to="a@x"))
    assert r.status_code == 422
    assert any(d["code"] == "self_reference"
               for d in r.json()["error"]["details"])

    r = import_(client, "mb2", eml("a@x", references=["x@y", "a@x"]))
    assert r.status_code == 422


def test_cycle_through_stored_chain_rejected(client):
    # rev1: real b -> c ; c and d placeholders, inferred c -> d
    import_(client, "mb",
            eml("b@x", references=["d@x", "c@x"]))
    # real c arrives claiming parent b while b's parent is c: cycle
    r = import_(client, "mb", eml("c@x", in_reply_to="b@x"))
    assert r.status_code == 422
    assert any(d["code"] == "cycle" for d in r.json()["error"]["details"])
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert t["revision"] == 1  # nothing advanced
    # c is still a missing placeholder
    assert set(t["missing"]) == {"c@x", "d@x"}


def test_cycle_within_single_batch_rejected(client):
    r = import_(client, "mb",
                eml("a@x", in_reply_to="b@x"),
                eml("b@x", in_reply_to="a@x"))
    assert r.status_code == 422
    assert any(d["code"] == "cycle" for d in r.json()["error"]["details"])


def test_empty_batch_rejected(client):
    r = client.post("/imports", data={"mailbox": "mb"})
    assert r.status_code == 422


def test_folded_and_commented_headers_parse(client):
    raw = (
        "Message-ID: (hello) <fold@x>\r\n"
        "References: <r1@x>\r\n"
        "   <r2@x> (a (nested) comment)\r\n"
        "Subject: folded\r\n\r\nbody"
    ).encode()
    r = import_(client, "mb", raw)
    assert r.status_code == 200
    t = client.get("/threads", params={"mailbox": "mb"}).json()
    assert set(roots(t)) == {"r1@x"}

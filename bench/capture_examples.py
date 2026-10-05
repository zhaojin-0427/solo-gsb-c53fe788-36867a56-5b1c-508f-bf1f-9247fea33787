"""Capture real request/response pairs for the README examples."""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pgserver
from fastapi.testclient import TestClient

from tests.eml_factory import eml


def pretty(resp) -> str:
    return json.dumps(resp.json(), ensure_ascii=False, indent=2)


def main() -> None:
    data_dir = tempfile.mkdtemp(prefix="pgdoc-")
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    try:
        os.environ["DATABASE_URL"] = server.get_uri()
        from app import db as dbmod
        dbmod.close_pool()
        from app.main import app

        with TestClient(app) as client:
            def post(name, *msgs):
                files = [("messages", (f"m{i}.eml", m, "message/rfc822"))
                         for i, m in enumerate(msgs)]
                return client.post("/imports", data={"mailbox": name}, files=files)

            r = post("demo", eml("reply-42@example.com",
                                 in_reply_to="ghost@example.com",
                                 subject="Re: Project plan"))
            print("### IMPORT 1 ###"); print(pretty(r))

            r = post("demo", eml("ghost@example.com",
                                 in_reply_to="thread-root@example.com",
                                 subject="Re: Project plan"))
            print("### IMPORT 2 (late parent, merges) ###"); print(pretty(r))

            r = post("demo", eml("thread-root@example.com",
                                 subject="Project plan"))
            print("### IMPORT 3 (root fills placeholder) ###"); print(pretty(r))

            r = client.get("/threads", params={"mailbox": "demo"})
            print("### THREADS latest ###"); print(pretty(r))

            r = client.get("/threads",
                           params={"mailbox": "demo", "revision": 1})
            print("### THREADS rev=1 ###"); print(pretty(r))

            r = post("demo", eml("ghost@example.com",
                                 in_reply_to="thread-root@example.com",
                                 subject="Re: Project plan"))
            print("### RE-IMPORT same bytes ###"); print(pretty(r))

            r = post("demo", eml("bad@example.com", in_reply_to="bad@example.com"))
            print("### SELF REF (422) ###")
            print(r.status_code); print(json.dumps(r.json(), indent=2))

            # --- conflicting References, order-independence demo ---
            m1 = eml("m1@example.com",
                     references=["alice@example.com", "p@example.com"],
                     subject="Re: Re: Project plan")
            m2 = eml("m2@example.com",
                     references=["bob@example.com", "p@example.com"],
                     subject="Re: Re: Project plan")
            r = post("conflict", m1, m2)
            print("### CONFLICT import [m1, m2] ###"); print(pretty(r))

            r = post("conflict", m2, m1)  # same set, reversed order
            print("### CONFLICT re-import reversed (duplicate) ###")
            print(pretty(r))

            r = client.get("/threads", params={"mailbox": "conflict"})
            print("### CONFLICT threads ###"); print(pretty(r))

            r = post("conflict", eml("p@example.com",
                                     in_reply_to="thread-root@example.com",
                                     subject="Re: Project plan"))
            print("### CONFLICT late parent of p ###"); print(pretty(r))

            r = client.get("/threads", params={"mailbox": "conflict"})
            print("### CONFLICT threads after merge ###"); print(pretty(r))
    finally:
        server.cleanup()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

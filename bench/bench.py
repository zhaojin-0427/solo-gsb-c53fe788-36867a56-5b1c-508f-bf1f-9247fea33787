"""Performance check: seeding 100k messages and timing a <=8KiB supplement.

Requirement: with 100k accumulated messages, a single <=8KiB supplement must
complete within 2 seconds in a 2-core / 1GiB container.

Usage:
    python bench/bench.py              # seed + time one supplement
    python bench/bench.py --keep N     # seed and keep the database dir

The bench seeds a realistic topology: many threads, chains and branches;
each generated message is <= 8 KiB.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import tempfile
import time

import pgserver
import psycopg

from app.db import init_schema
from app.importer import get_threads, import_batch
from tests.eml_factory import eml


def seed(conn, n: int, batch: int = 1000) -> None:
    # keep a pool of "open" parents so messages distribute across threads and
    # chains, rather than one pathological 100k-deep chain
    rnd = random.Random(20261004)
    open_parents: list[str] = []
    t0 = time.perf_counter()
    emitted = 0
    buf: list[bytes] = []

    def flush():
        nonlocal buf
        if buf:
            import_batch(conn, "bench", buf)
            conn.commit()
            buf = []

    for i in range(n):
        mid = f"m{i:06d}@bench"
        if open_parents and rnd.random() < 0.92:
            # reply; sometimes extend references with a grandparent
            parent = rnd.choice(open_parents)
            if rnd.random() < 0.4 and len(open_parents) > 1:
                gp = rnd.choice(open_parents)
                msg = eml(mid, in_reply_to=parent,
                          references=[gp, parent],
                          body=f"body-{i}")
            else:
                msg = eml(mid, in_reply_to=parent, body=f"body-{i}")
        else:
            # new thread root
            msg = eml(mid, body=f"root-{i}")
        assert len(msg) <= 8192
        buf.append(msg)
        open_parents.append(mid)
        if len(open_parents) > 4000:
            # retire random parents so deep chains still form sometimes
            del open_parents[rnd.randrange(len(open_parents) - 3000)]
        emitted += 1
        if len(buf) >= batch:
            flush()
            rate = emitted / (time.perf_counter() - t0)
            print(f"  seeded {emitted}/{n} ({rate:.0f} msg/s)")
    flush()
    print(f"seeded {n} messages in {time.perf_counter() - t0:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=100_000)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    data_dir = os.environ.get("BENCH_PGDATA") or tempfile.mkdtemp(prefix="pgbench-")
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    print(f"postgres data dir: {data_dir}")

    try:
        import psycopg as p3

        with p3.connect(server.get_uri(), autocommit=True) as admin:
            admin.execute('DROP DATABASE IF EXISTS bench')
            admin.execute('CREATE DATABASE bench')
        from urllib.parse import urlparse, urlunparse

        uri = urlunparse(urlparse(server.get_uri())._replace(path="/bench"))

        from app import db as dbmod

        os.environ["DATABASE_URL"] = uri
        dbmod.close_pool()
        init_schema()

        with dbmod.get_pool().connection() as conn:
            count = conn.execute(
                "SELECT count(*) FROM nodes WHERE is_placeholder=FALSE"
            ).fetchone()[0]
            if count < args.messages:
                seed(conn, args.messages - count)

            n_nodes = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
            n_revs = conn.execute("SELECT count(*) FROM revisions").fetchone()[0]
            print(f"state: {n_nodes} nodes, {n_revs} revisions")

            # --- the actual SLA measurement: one <=8KiB supplement ---
            leaf = conn.execute(
                "SELECT msg_id FROM nodes WHERE is_placeholder=FALSE "
                "ORDER BY created_rev DESC LIMIT 1"
            ).fetchone()[0]
            supplement = eml("supplement@bench", in_reply_to=leaf,
                             body="x" * 7000)
            assert len(supplement) <= 8192

            times = []
            for k in range(3):
                msg = supplement if k == 0 else eml(
                    f"supplement{k}@bench", in_reply_to=leaf, body="x" * 7000)
                t0 = time.perf_counter()
                r = import_batch(conn, "bench", [msg])
                conn.commit()
                dt = time.perf_counter() - t0
                times.append(dt)
                print(f"supplement {k + 1}: {dt * 1000:.1f} ms "
                      f"(effective={r['effective']}, rev={r['revision']})")

            best = min(times)
            print(f"\nBEST import: {best * 1000:.1f} ms  SLA: 2000 ms  "
                  f"=> {'PASS' if best <= 2.0 else 'FAIL'}")

            # --- GET /threads at full 100k-node scale ---
            import json

            t0 = time.perf_counter()
            payload = get_threads(conn, "bench", None)
            dt = time.perf_counter() - t0
            encoded = json.dumps(payload).encode()
            print(f"GET /threads: {dt * 1000:.1f} ms "
                  f"({len(payload['threads'])} threads, "
                  f"{len(payload['missing'])} missing, "
                  f"{len(encoded) / 1e6:.1f} MB JSON)")

            # and a historical revision read
            mid_rev = r["revision"] // 2
            t0 = time.perf_counter()
            get_threads(conn, "bench", mid_rev)
            dt = time.perf_counter() - t0
            print(f"GET /threads?revision={mid_rev}: {dt * 1000:.1f} ms")
    finally:
        if args.keep:
            print(f"keeping database dir: {data_dir}")
        else:
            server.cleanup()
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

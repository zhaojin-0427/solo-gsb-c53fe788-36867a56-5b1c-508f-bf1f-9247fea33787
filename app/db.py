"""Database connection pool and schema bootstrap."""
from __future__ import annotations

import os
from pathlib import Path

from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get(
            "DATABASE_URL", "postgresql://mails:mails@db:5432/mails"
        )
        _pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", "10")),
            configure=_configure_conn,
            open=True,
        )
    return _pool


def _configure_conn(conn) -> None:
    # psycopg 3: we manage transactions explicitly; the pool's connection
    # context still rolls back on exception.
    conn.autocommit = False


def init_schema() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        conn.execute(sql)
        conn.commit()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

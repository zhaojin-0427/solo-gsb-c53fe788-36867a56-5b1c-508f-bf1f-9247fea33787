"""Shared fixtures: an embedded PostgreSQL and a FastAPI test client."""
from __future__ import annotations

import os
import shutil
import tempfile

import pgserver
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def pg_uri():
    data_dir = tempfile.mkdtemp(prefix="pgtest-")
    try:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
        yield server.get_uri()
        server.cleanup()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture()
def client(pg_uri, monkeypatch):
    # fresh isolated database per test function
    db = f"db_{os.urandom(6).hex()}"
    import psycopg

    with psycopg.connect(pg_uri, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db}"')

    from urllib.parse import urlparse, urlunparse

    parts = urlparse(pg_uri)
    uri = urlunparse(parts._replace(path=f"/{db}"))
    monkeypatch.setenv("DATABASE_URL", uri)

    # reset the lazy pool so it binds to this database
    from app import db as dbmod

    dbmod.close_pool()

    from app.main import app

    with TestClient(app) as c:
        yield c

    dbmod.close_pool()

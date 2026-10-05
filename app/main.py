"""HTTP layer for the email conversation reconstruction service."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from . import importer
from .db import close_pool, get_pool, init_schema
from .importer import BatchRejected


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_schema()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(
    title="Email Conversation Reconstruction API",
    version="1.0.0",
    description="Imports raw EML batches per mailbox and reconstructs "
                "revisioned conversation trees.",
    lifespan=lifespan,
)


@app.exception_handler(BatchRejected)
async def _rejected_handler(_request: Request, exc: BatchRejected) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": exc.code, "details": exc.details}},
    )


@app.get("/health")
def health() -> dict:
    with get_pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/imports")
async def create_import(
    mailbox: str = Form(..., description="Mailbox identifier; namespaces are independent."),
    messages: list[UploadFile] = File(
        ..., description="One or more raw EML files; batch order is irrelevant."
    ),
) -> dict:
    mailbox = mailbox.strip()
    if not mailbox:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_mailbox", "message": "mailbox must be non-empty"},
        )
    if not messages:
        raise HTTPException(status_code=422, detail="at least one message is required")

    raw_items = [await f.read() for f in messages]
    if any(not item for item in raw_items):
        raise HTTPException(
            status_code=422,
            detail={"code": "malformed_eml", "message": "empty EML payload"},
        )

    with get_pool().connection() as conn:
        try:
            result = importer.import_batch(conn, mailbox, raw_items)
        except BatchRejected:
            raise
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    return result


@app.get("/threads")
def list_threads(
    mailbox: str = Query(..., description="Mailbox identifier."),
    revision: int | None = Query(
        None, ge=1, description="Revision to read; defaults to the latest."
    ),
) -> dict:
    mailbox = mailbox.strip()
    if not mailbox:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_mailbox", "message": "mailbox must be non-empty"},
        )
    with get_pool().connection() as conn:
        try:
            return importer.get_threads(conn, mailbox, revision)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={"code": "unknown_mailbox", "mailbox": mailbox},
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "unknown_revision", "revision": exc.args[0]},
            )

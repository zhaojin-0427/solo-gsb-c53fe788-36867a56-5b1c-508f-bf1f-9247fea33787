"""Helpers for building raw EML messages."""
from __future__ import annotations


def eml(
    msg_id: str | None,
    *,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    subject: str = "hi",
    body: str = "hello",
    fold_headers: bool = False,
) -> bytes:
    lines = []
    if msg_id is not None:
        lines.append(f"Message-ID: <{msg_id}>")
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: <{in_reply_to}>")
    if references:
        if fold_headers:
            joined = "\r\n ".join(f"<{r}>" for r in references)
            lines.append(f"References: {joined}")
        else:
            lines.append("References: " + " ".join(f"<{r}>" for r in references))
    lines.append(f"Subject: {subject}")
    lines.append("From: a@example.com")
    lines.append("To: b@example.com")
    lines.append("")
    lines.append(body)
    return ("\r\n".join(lines)).encode("utf-8")

"""EML parsing with the standard-library :mod:`email` package.

Only the threading headers matter here:

* ``Message-ID``   -- identity of the message (required)
* ``In-Reply-To``  -- unique reply identifier; when present it takes
                      precedence over References
* ``References``   -- chain of parent ids, newest last; the *last* id is the
                      parent when In-Reply-To is absent

Header values use RFC 5322 CFWS: angle-addr tokens, comments in parentheses,
folding whitespace. The stdlib parser handles unfolding/folding; comment
stripping and token extraction are done here because
``email.utils.getaddresses`` silently drops malformed/odd ids.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import Message


@dataclass(frozen=True)
class ParsedMessage:
    msg_id: str
    parent_id: str | None
    references: tuple[str, ...]


class ParseError(ValueError):
    """The raw EML cannot be imported (malformed or missing identity)."""


def parse_eml(raw: bytes) -> ParsedMessage:
    if not isinstance(raw, (bytes, bytearray)):
        raise ParseError("EML payload must be raw bytes")
    raw = bytes(raw)

    # Header-only parsing: bodies are irrelevant for threading, and ignoring
    # them keeps parsing cheap regardless of attachment size.
    msg = message_from_bytes_head(raw)

    ids = extract_ids(msg.get("message-id"))
    if not ids:
        raise ParseError("missing or empty Message-ID header")
    msg_id = ids[0]

    irt = extract_ids(msg.get("in-reply-to"))
    refs: list[str] = []
    for value in msg.get_all("references", []):
        refs.extend(extract_ids(value))

    parent = irt[0] if irt else (refs[-1] if refs else None)
    return ParsedMessage(msg_id=msg_id, parent_id=parent, references=tuple(refs))


def message_from_bytes_head(raw: bytes) -> Message:
    """Parse only the header block, tolerating malformed bodies."""
    lines: list[bytes] = []
    for line in raw.splitlines():
        if line.strip() == b"":
            break
        lines.append(line)
    head = b"\n".join(lines)
    return message_from_bytes(head, policy=policy.default)


def _strip_comments(text: str) -> str:
    """Remove RFC 5322 comments (nested parentheses, quoted-pairs)."""
    out: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and depth:
            i += 2  # quoted-pair inside comment
            continue
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth == 0:
            out.append(ch)
        i += 1
    return "".join(out)


_ANGLE_RE = re.compile(r"<([^<>]*)>")


def extract_ids(value: str | None) -> list[str]:
    """Extract message identifiers from a CFWS header value, in order.

    Prefers ``<...>`` tokens; falls back to bare whitespace-separated tokens
    when no angle brackets are present. Duplicates are preserved because the
    position (especially the last element) is semantically meaningful.
    """
    if value is None:
        return []
    text = _strip_comments(str(value))
    angled = _ANGLE_RE.findall(text)
    if angled:
        return [tok.strip() for tok in angled if tok.strip()]
    return [tok for tok in re.split(r"\s+", text.strip()) if tok]

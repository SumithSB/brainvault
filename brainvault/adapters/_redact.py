"""Shared redaction helpers for hook payloads (Claude Code, Cursor, …)."""

from __future__ import annotations

import re

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)Authorization:\s*\S+"), "Authorization: <redacted>"),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "<redacted>"),
)


def redact_sensitive(text: str, max_len: int = 500) -> str:
    """Redact secrets, then cap length.

    Slicing to ``max_len`` first can cut an ``sk-`` key below the 20-character
    minimum, so a prefix of the secret is kept. Finish the token that begins
    inside the cap (secrets are non-whitespace), match, then truncate.
    """
    if max_len < 0:
        max_len = 0
    end = min(len(text), max_len)
    while end < len(text) and not text[end].isspace():
        end += 1
    t = text[:end]
    for pat, sub in _REDACT_PATTERNS:
        t = pat.sub(sub, t)
    return t[:max_len]

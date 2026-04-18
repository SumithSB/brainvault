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
    t = text[:max_len]
    for pat, sub in _REDACT_PATTERNS:
        t = pat.sub(sub, t)
    return t

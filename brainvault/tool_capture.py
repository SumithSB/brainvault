"""
brainvault/tool_capture.py — PostToolUse hook handler.

Invoked by Claude Code after every Write / Edit / Bash / TodoWrite / NotebookEdit
call.  Payload arrives via stdin as a single JSON object.

Design goals:
  - <20 ms latency — one INSERT per event, no reads, no embedding
  - Never crash — all errors swallowed (hook failure would break Claude Code)
  - No user-visible output
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CAPTURED_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "Bash", "TodoWrite", "NotebookEdit"})

# Prevent pathological stdin from stalling the hook (DoS).
_MAX_PAYLOAD_BYTES = 256 * 1024

# Best-effort redaction for replay buffer (local DB only; still avoid persisting obvious secrets).
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)Authorization:\s*\S+"), "Authorization: <redacted>"),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "<redacted>"),
)


def _redact_sensitive(text: str, max_len: int = 500) -> str:
    t = text[:max_len]
    for pat, sub in _REDACT_PATTERNS:
        t = pat.sub(sub, t)
    return t


def _read_hook_payload() -> dict:
    """Read and parse the JSON payload from stdin."""
    stdin = sys.stdin
    buf = getattr(stdin, "buffer", None)
    if buf is not None:
        raw = buf.read(_MAX_PAYLOAD_BYTES + 1)
    else:
        chunk = stdin.read(_MAX_PAYLOAD_BYTES + 1)
        raw = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
    if len(raw) > _MAX_PAYLOAD_BYTES:
        raise ValueError("hook payload exceeds size limit")
    return json.loads(raw.decode("utf-8", errors="replace"))


def _summarize_input(tool_name: str, tool_input: dict) -> str:
    """Produce a compact, human-readable description of what the tool did."""
    if tool_name in ("Write", "NotebookEdit"):
        return f"→ {tool_input.get('file_path', '?')}"
    if tool_name == "Edit":
        fp = tool_input.get("file_path", "?")
        old = tool_input.get("old_string", "")
        snippet = old[:60].replace("\n", " ") if old else ""
        return f"→ {fp}" + (f"  [{snippet}…]" if snippet else "")
    if tool_name == "Bash":
        return _redact_sensitive(tool_input.get("command", "") or "", max_len=300)
    if tool_name == "TodoWrite":
        todos = tool_input.get("todos", [])
        pending = [t for t in todos if t.get("status") == "pending"]
        return f"{len(todos)} todos ({len(pending)} pending)"
    return _redact_sensitive(str(tool_input), max_len=200)


def _summarize_output(tool_name: str, tool_response: dict) -> str:
    """Extract a terse output summary (error detection, exit codes)."""
    if tool_name == "Bash":
        exit_code = tool_response.get("exit_code")
        stderr = _redact_sensitive((tool_response.get("stderr") or "")[:100], max_len=100)
        if exit_code and exit_code != 0:
            return f"exit={exit_code} {stderr}".strip()
        return f"exit={exit_code or 0}"
    return ""


def _derive_session_id(payload: dict) -> str:
    """Stable session ID from the JSONL transcript path stem."""
    tp = payload.get("transcript_path", "")
    if tp:
        return Path(tp).stem
    import datetime

    return datetime.date.today().isoformat()


def _infer_project(payload: dict) -> str | None:
    """Best-effort project inference from the transcript path."""
    tp = payload.get("transcript_path", "")
    if not tp:
        return None
    # Transcript paths look like ~/.claude/projects/-Users-foo-Projects-myproject/<uuid>.jsonl
    # The parent directory encodes the working directory.
    parent = Path(tp).parent.name
    if parent.startswith("-"):
        # Decode path: replace '-' with '/' and strip leading slash
        decoded = parent.replace("-", "/").lstrip("/")
        return Path(decoded).name  # last path component = project name
    return None


def process_event(payload: dict) -> None:
    """Validate payload and write one event row — the hot path."""
    tool_name = payload.get("tool_name", "")
    if tool_name not in CAPTURED_TOOLS:
        return

    tool_input: dict = payload.get("tool_input") or {}
    tool_response: dict = payload.get("tool_response") or {}

    session_id = _derive_session_id(payload)
    project = _infer_project(payload)
    input_summary = _summarize_input(tool_name, tool_input)
    output_summary = _summarize_output(tool_name, tool_response)

    from brainvault import db

    db.init_db()
    db.record_tool_event(
        session_id=session_id,
        tool_name=tool_name,
        input_summary=input_summary,
        output_summary=output_summary,
        project=project,
    )


def run() -> None:
    """Entry point — called by Claude Code PostToolUse hook."""
    try:
        payload = _read_hook_payload()
    except Exception:
        return  # malformed payload — nothing we can do

    try:
        process_event(payload)
    except Exception:
        pass  # Never crash the hook


if __name__ == "__main__":
    run()

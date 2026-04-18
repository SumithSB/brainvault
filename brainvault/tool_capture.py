"""
brainvault/tool_capture.py — Post-tool hook handler.

Invoked by Claude Code (PostToolUse) or Cursor (``postToolUse``, ``afterFileEdit``,
``afterShellExecution``). Payload arrives via stdin as a single JSON object.

Design goals:
  - <20 ms latency — one INSERT per event, no reads, no embedding
  - Never crash — all errors swallowed (hook failure would break the agent host)
  - No user-visible output

Payload normalisation is delegated to the first ``AgentAdapter`` whose
``owns_payload`` returns True (Claude Code first, then Cursor).
"""

from __future__ import annotations

import json
import sys

from brainvault.adapters import ALL_ADAPTERS
from brainvault.adapters import claude_code as _claude_code_adapter

# Re-export for tests / callers that imported from tool_capture historically.
CAPTURED_TOOLS = _claude_code_adapter.CAPTURED_TOOLS

# Prevent pathological stdin from stalling the hook (DoS).
_MAX_PAYLOAD_BYTES = 256 * 1024


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


def process_event(payload: dict) -> None:
    """Route payload to the owning adapter and write one event row — the hot path."""
    for cls in ALL_ADAPTERS:
        adapter = cls()
        if not adapter.owns_payload(payload):
            continue
        event = adapter.event_from_payload(payload)
        if event is None:
            return

        from brainvault import db

        db.init_db()
        db.record_tool_event(
            session_id=event.session_id,
            tool_name=event.tool_name,
            input_summary=event.input_summary,
            output_summary=event.output_summary,
            project=event.project,
            source_agent=adapter.name,
        )
        return


def run() -> None:
    """Entry point — called by Claude Code PostToolUse or Cursor tool hooks."""
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

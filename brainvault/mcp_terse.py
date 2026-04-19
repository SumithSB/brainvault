"""
Optional terse formatting for MCP tool *responses*.

Goal: fewer tokens in the tool-result channel when the model re-reads outputs, without
losing the facts the agent needs. We use shorter previews, delimiter-style lines, and
compact status tokens instead of markdown-heavy prose. That is orthogonal to how
verbose the assistant’s own replies are — it only shapes what the server returns.

Enable with BRAINVAULT_MCP_TERSE set to a truthy value (see ``mcp_terse_enabled``).
"""

from __future__ import annotations

import os

# When terse: tighter preview caps (caller may still pass explicit max_chars to search_memory).
TERSE_MEMORY_PREVIEW_CHARS = 200
VERBOSE_MEMORY_PREVIEW_CHARS = 400


def mcp_terse_enabled() -> bool:
    v = (os.environ.get("BRAINVAULT_MCP_TERSE") or "").strip().lower()
    return v in ("1", "true", "yes", "on", "terse")


def effective_search_max_chars(requested_max: int, *, default_verbose: int) -> int:
    """If user left default, terse mode lowers the cap; explicit larger values are respected."""
    if not mcp_terse_enabled():
        return requested_max
    if requested_max == default_verbose:
        return min(TERSE_MEMORY_PREVIEW_CHARS, default_verbose)
    return requested_max

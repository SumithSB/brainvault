"""Tests for brainvault.mcp_terse preview caps."""

from __future__ import annotations

import pytest

from brainvault.mcp_terse import (
    TERSE_MEMORY_PREVIEW_CHARS,
    VERBOSE_MEMORY_PREVIEW_CHARS,
    effective_search_max_chars,
    mcp_terse_enabled,
)


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("YES", True),
        (" On ", True),
        ("terse", True),
    ],
)
def test_mcp_terse_enabled_truthy_values(monkeypatch, raw, enabled):
    if raw is None:
        monkeypatch.delenv("BRAINVAULT_MCP_TERSE", raising=False)
    else:
        monkeypatch.setenv("BRAINVAULT_MCP_TERSE", raw)
    assert mcp_terse_enabled() is enabled


def test_effective_search_max_chars_rewrites_only_the_default(monkeypatch):
    """Terse mode lowers the untouched default. An explicit cap stays as given."""
    default = VERBOSE_MEMORY_PREVIEW_CHARS
    monkeypatch.delenv("BRAINVAULT_MCP_TERSE", raising=False)
    assert effective_search_max_chars(default, default_verbose=default) == default
    assert effective_search_max_chars(80, default_verbose=default) == 80

    monkeypatch.setenv("BRAINVAULT_MCP_TERSE", "1")
    terse_cap = effective_search_max_chars(default, default_verbose=default)
    assert terse_cap == min(TERSE_MEMORY_PREVIEW_CHARS, default)
    assert effective_search_max_chars(800, default_verbose=default) == 800
    assert effective_search_max_chars(80, default_verbose=default) == 80


def test_search_memory_terse_cap_unless_caller_overrides(monkeypatch):
    from brainvault.mcp_server import save_memory, search_memory

    sentence = "celery retry budget decision "
    body = (sentence * 40).strip()
    monkeypatch.delenv("BRAINVAULT_MCP_TERSE", raising=False)
    saved = save_memory(body, "note")
    memory_id = saved.split("Memory ID: ")[1].split(" ")[0]

    monkeypatch.setenv("BRAINVAULT_MCP_TERSE", "yes")
    terse = search_memory("celery retry budget")
    expected = body[:TERSE_MEMORY_PREVIEW_CHARS].rstrip() + f"…id={memory_id[:8]}"
    assert expected in terse
    assert body[: TERSE_MEMORY_PREVIEW_CHARS + 1] not in terse

    wide = search_memory("celery retry budget", max_chars=500)
    expected_wide = body[:500].rstrip() + f"…id={memory_id[:8]}"
    assert expected_wide in wide
    assert body[:501] not in wide

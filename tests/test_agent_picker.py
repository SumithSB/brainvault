"""Tests for brainvault.agent_picker (TTY checklist; uses injected read_byte)."""

from __future__ import annotations

from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
from brainvault.agent_picker import KEY_ENTER, KEY_SPACE, pick_agents_checklist


def test_checklist_enter_with_all_selected_returns_none():
    a1, a2 = ClaudeCodeAdapter(), CursorAdapter()
    keys = iter([KEY_ENTER])

    def read_byte() -> int:
        return next(keys)

    assert pick_agents_checklist([a1, a2], read_byte=read_byte) is None


def test_checklist_space_unselects_first_then_enter_returns_subset():
    a1, a2 = ClaudeCodeAdapter(), CursorAdapter()
    keys = iter([KEY_SPACE, KEY_ENTER])

    def read_byte() -> int:
        return next(keys)

    assert pick_agents_checklist([a1, a2], read_byte=read_byte) == ["cursor"]


def test_checklist_q_returns_empty():
    a1, a2 = ClaudeCodeAdapter(), CursorAdapter()
    keys = iter([ord("q")])

    def read_byte() -> int:
        return next(keys)

    assert pick_agents_checklist([a1, a2], read_byte=read_byte) == []


def test_checklist_single_agent_returns_none_immediately():
    a1 = ClaudeCodeAdapter()
    assert pick_agents_checklist([a1]) is None

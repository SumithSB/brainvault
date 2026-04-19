"""Tests for brainvault.agent_picker (TTY checklist; uses injected read_byte)."""

from __future__ import annotations

from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
from brainvault.agent_picker import (
    KEY_DOWN,
    KEY_ENTER,
    KEY_SPACE,
    KEY_UP,
    pick_agents_checklist,
)


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


def test_checklist_arrow_down_moves_then_space_deselects_second():
    """
    Regression: _read_byte_unix was comparing bytes to ord(...) (int), so
    arrow keys silently fell through and navigation didn't work. This test
    uses the injected read_byte path (same KEY_UP/KEY_DOWN sentinels the
    real reader returns) to confirm arrow navigation + toggle works.
    """
    a1, a2 = ClaudeCodeAdapter(), CursorAdapter()
    keys = iter([KEY_DOWN, KEY_SPACE, KEY_ENTER])

    def read_byte() -> int:
        return next(keys)

    # Both start selected; down to index 1, space deselects cursor, enter confirms.
    assert pick_agents_checklist([a1, a2], read_byte=read_byte) == ["claude_code"]


def test_checklist_arrow_up_wraps_and_deselects_first():
    a1, a2 = ClaudeCodeAdapter(), CursorAdapter()
    keys = iter([KEY_UP, KEY_SPACE, KEY_ENTER])

    def read_byte() -> int:
        return next(keys)

    # Index starts at 0; up wraps to 1, space deselects cursor, enter confirms.
    assert pick_agents_checklist([a1, a2], read_byte=read_byte) == ["claude_code"]


def test_read_byte_unix_decodes_arrow_escape_sequence(monkeypatch):
    """
    End-to-end: feed raw ESC [ A bytes through the real unix reader.
    Was silently broken because bytes were compared against ord(...) ints.
    """
    import sys as _sys
    import types

    from brainvault import agent_picker as ap

    byte_stream = iter([b"\x1b", b"[", b"A"])

    class _FakeBuffer:
        def read(self, n: int) -> bytes:
            return next(byte_stream)

    class _FakeStdin:
        buffer = _FakeBuffer()

        def fileno(self) -> int:
            return 0

    monkeypatch.setattr(ap.sys, "stdin", _FakeStdin())

    fake_termios = types.ModuleType("termios")
    fake_termios.tcgetattr = lambda fd: None
    fake_termios.tcsetattr = lambda fd, when, attrs: None
    fake_termios.TCSADRAIN = 0
    fake_tty = types.ModuleType("tty")
    fake_tty.setraw = lambda fd: None
    monkeypatch.setitem(_sys.modules, "termios", fake_termios)
    monkeypatch.setitem(_sys.modules, "tty", fake_tty)

    assert ap._read_byte_unix() == ap.KEY_UP

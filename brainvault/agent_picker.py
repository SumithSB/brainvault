"""
TTY checklist for choosing which installed coding agents to target (e.g. brainvault install).

Space toggles selection, Enter confirms, j/k or arrow keys move. Falls back to line-based
prompt in cli.py when stdin/stdout is not a TTY.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brainvault.adapters.base import AgentAdapter

KEY_ENTER = 13
KEY_LF = 10
KEY_ESC = 27
KEY_SPACE = 32
KEY_CTRL_C = 3
# Sentinels for arrow keys (outside byte range).
KEY_UP = 0x1000
KEY_DOWN = 0x1001


def pick_agents_checklist(
    detected: list[AgentAdapter],
    *,
    read_byte: Callable[[], int] | None = None,
) -> list[str] | None:
    """
    Interactive multiselect. All agents start selected (same default as "Enter for all").

    Returns:
        None — all detected agents (confirm with all still selected).
        [] — user cancelled (q, Ctrl+C, or no selection confirmed).
        [names...] — subset of adapter machine names.
    """
    if len(detected) < 2:
        return None

    names = [a.name for a in detected]
    labels = [a.display_name for a in detected]
    n = len(detected)
    selected = [True] * n
    index = 0
    line_count = 0

    def read_b() -> int:
        if read_byte is not None:
            return read_byte()
        if os.name == "nt":
            return _read_byte_windows()
        return _read_byte_unix()

    def draw() -> None:
        nonlocal line_count
        lines = [
            "Select coding agents (up/down or j/k move, SPACE toggles, ENTER confirms, a=all, n=none, q=quit):",
            "",
        ]
        for i in range(n):
            mark = "[x]" if selected[i] else "[ ]"
            cur = ">" if i == index else " "
            lines.append(f" {cur} {mark}  {labels[i]}  ({names[i]})")
        lines.append("")
        lines.append("  [x] = will install   [ ] = skip")
        text = "\n".join(lines)
        out = sys.stdout
        if line_count:
            out.write(f"\033[{line_count}A\033[J")
        out.write(text)
        out.flush()
        line_count = len(lines)

    while True:
        draw()
        code = read_b()
        if code in (KEY_ENTER, KEY_LF):
            if not any(selected):
                sys.stdout.write("\n  Nothing selected — choose at least one, or q to quit.\n")
                sys.stdout.flush()
                line_count = 0
                continue
            if all(selected):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return None
            out = [names[i] for i in range(n) if selected[i]]
            sys.stdout.write("\n")
            sys.stdout.flush()
            return out
        if code in (KEY_CTRL_C,) or code in (ord("q"), ord("Q")):
            sys.stdout.write("\n  Aborted.\n")
            sys.stdout.flush()
            return []
        if code in (ord("a"), ord("A")):
            selected[:] = [True] * n
            continue
        if code in (ord("n"), ord("N")):
            selected[:] = [False] * n
            continue
        if code == KEY_SPACE:
            selected[index] = not selected[index]
            continue
        if code in (ord("j"), KEY_DOWN):
            index = (index + 1) % n
            continue
        if code in (ord("k"), KEY_UP):
            index = (index - 1) % n
            continue


def _read_byte_unix() -> int:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = sys.stdin.buffer.read(1)
        if not b:
            return KEY_CTRL_C
        c0 = b[0]
        if c0 != KEY_ESC:
            return c0
        # Arrow keys: ESC [ A / ESC [ B
        b2 = sys.stdin.buffer.read(1)
        b3 = sys.stdin.buffer.read(1)
        if b2 == ord("[") and b3 == ord("A"):
            return KEY_UP
        if b2 == ord("[") and b3 == ord("B"):
            return KEY_DOWN
        return c0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_byte_windows() -> int:
    import msvcrt

    b = msvcrt.getch()
    if not b:
        return KEY_CTRL_C
    if b == b"\xe0":
        b2 = msvcrt.getch()
        if b2 == b"H":
            return KEY_UP
        if b2 == b"P":
            return KEY_DOWN
        return b2[0] if b2 else 0
    return b[0]

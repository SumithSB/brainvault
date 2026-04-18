"""
brainvault.adapters — agent-specific install / capture / parse logic.

Each adapter implements AgentAdapter for a concrete coding-agent host
(Claude Code, Cursor, ...). The rest of brainvault is agent-neutral and reads
adapters through this package only.
"""

from brainvault.adapters.base import (
    AgentAdapter,
    HookResult,
    SessionEvent,
)
from brainvault.adapters.claude_code import ClaudeCodeAdapter
from brainvault.adapters.cursor import CursorAdapter

ALL_ADAPTERS: tuple[type[AgentAdapter], ...] = (ClaudeCodeAdapter, CursorAdapter)


def all_adapters() -> list[AgentAdapter]:
    """Instantiate every known adapter. Cheap — they do no I/O in __init__."""
    return [cls() for cls in ALL_ADAPTERS]


def installed_adapters() -> list[AgentAdapter]:
    """Return only the adapters whose host agent is detected on disk."""
    return [a for a in all_adapters() if a.is_installed()]


def resolve(names: list[str] | None) -> list[AgentAdapter]:
    """
    Map CLI `--agent` selectors to adapter instances.

    - None or ["auto"]: every installed adapter
    - ["all"]: every known adapter, whether installed or not
    - ["claude_code", "cursor", ...]: the named adapters (unknown names raise ValueError)
    """
    if not names or names == ["auto"]:
        return installed_adapters()
    if names == ["all"]:
        return all_adapters()

    by_name = {cls.name: cls for cls in ALL_ADAPTERS}
    resolved: list[AgentAdapter] = []
    unknown: list[str] = []
    for n in names:
        # Accept friendly aliases
        key = n.strip().lower().replace("-", "_")
        if key == "claude":
            key = "claude_code"
        cls = by_name.get(key)
        if cls is None:
            unknown.append(n)
        else:
            resolved.append(cls())
    if unknown:
        raise ValueError(
            f"Unknown agent(s): {', '.join(unknown)}. Known: {', '.join(sorted(by_name.keys()))}"
        )
    return resolved


__all__ = [
    "AgentAdapter",
    "HookResult",
    "SessionEvent",
    "ClaudeCodeAdapter",
    "CursorAdapter",
    "ALL_ADAPTERS",
    "all_adapters",
    "installed_adapters",
    "resolve",
]

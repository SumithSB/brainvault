"""
brainvault/adapters/base.py — AgentAdapter ABC and shared dataclasses.

One adapter per supported coding-agent host. Adapters own everything that
differs between hosts: config-file paths, instruction-file paths, hook shapes,
transcript formats, project-name encodings. The rest of brainvault
(db, embeddings, graph, MCP server) is adapter-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class _HomeRelativePath:
    """Descriptor: ``Path.home() / *parts`` resolved at access time.

    Class attributes like ``SETTINGS_PATH = _HomeRelativePath('.claude', 'settings.json')``
    follow the current home directory (including tests that patch ``HOME`` / ``USERPROFILE``).
    Assigning a plain :class:`pathlib.Path` on the class replaces the descriptor, which tests rely on.
    """

    __slots__ = ("_parts",)

    def __init__(self, *parts: str) -> None:
        self._parts = parts

    def __get__(self, obj: object | None, objtype: type | None = None) -> Path:
        p = Path.home()
        for part in self._parts:
            p = p / part
        return p


@dataclass
class HookResult:
    """Outcome of (un)registering hooks for an adapter."""

    registered: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # hooks removed during unregister
    skipped: list[str] = field(default_factory=list)  # already present / already absent
    unsupported: list[str] = field(default_factory=list)  # host has no such hook

    @property
    def any_changed(self) -> bool:
        return bool(self.registered or self.removed)


@dataclass
class SessionEvent:
    """Normalized tool-call event — what the session replay buffer stores."""

    session_id: str
    tool_name: str
    input_summary: str = ""
    output_summary: str = ""
    project: str | None = None


class AgentAdapter(ABC):
    """Abstract base for a concrete agent host (Claude Code, Cursor, ...).

    Adapters must be cheap to instantiate — no filesystem I/O in __init__.
    Detection, patching, and parsing happen through explicit methods so callers
    can opt into each step.
    """

    # Short machine name used in DB source_agent column and CLI flags.
    # Lowercase, snake_case, stable across versions.
    name: str = ""

    # Human-readable name for install / doctor output.
    display_name: str = ""

    # --- detection & install ----------------------------------------------

    @abstractmethod
    def is_installed(self) -> bool:
        """Return True if the host agent appears to be installed on this machine."""

    @abstractmethod
    def register_mcp(self) -> bool:
        """Add brainvault to the host's MCP server list. True if newly added, False if skipped.

        Implementations must parse existing config defensively, back up on
        parse errors, and never overwrite with an empty object.
        """

    @abstractmethod
    def unregister_mcp(self) -> bool:
        """Remove the brainvault MCP entry. True if removed, False if not present."""

    @abstractmethod
    def inject_instructions(self) -> str:
        """Write or upgrade the managed instruction block.

        Returns one of: 'injected', 'upgraded', 'current', 'unsupported'.
        """

    @abstractmethod
    def strip_instructions(self) -> str:
        """Remove the managed instruction block.

        Returns one of: 'removed', 'not-present', 'missing-file', 'unsupported'.
        """

    def register_hooks(self) -> HookResult:
        """Register Stop / PostToolUse hooks. Default: unsupported for hosts without hooks."""
        return HookResult(unsupported=["Stop", "PostToolUse"])

    def unregister_hooks(self) -> HookResult:
        """Unregister hooks. Default: unsupported."""
        return HookResult(unsupported=["Stop", "PostToolUse"])

    # --- transcript / session capture -------------------------------------

    def session_dir(self) -> Path | None:
        """Directory containing JSONL session transcripts, or None if unavailable."""
        return None

    def parse_session_file(self, path: Path) -> list[str]:
        """Extract continuation-style summary chunks. Empty list if not supported."""
        return []

    def recent_session_files(self, max_age_seconds: int = 300) -> list[Path]:
        """JSONL transcripts touched within max_age_seconds, newest first. Default: none."""
        return []

    def extract_project_name(self, session_path: Path) -> str:
        """Derive the project name from a session transcript path. Default: directory basename."""
        return session_path.parent.name

    # --- hook payload routing ---------------------------------------------

    def owns_payload(self, payload: dict) -> bool:
        """Does this adapter recognise a PostToolUse payload shape?"""
        return False

    def event_from_payload(self, payload: dict) -> SessionEvent | None:
        """Normalize a hook payload into a SessionEvent. Return None to drop."""
        return None

    # --- diagnostics ------------------------------------------------------

    def health_checks(self) -> list[tuple[str, bool, str]]:
        """Return a list of (label, ok, detail) tuples for `brainvault doctor`.

        Default: a single is_installed probe. Adapters should override to check
        MCP registration, hook entries, interpreter path, instruction block, etc.
        """
        return [(f"{self.display_name} detected", self.is_installed(), "")]

    # --- pretty printing --------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover — cosmetic
        return f"<{self.__class__.__name__} name={self.name}>"

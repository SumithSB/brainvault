"""
brainvault/adapters/claude_code.py — Claude Code adapter.

Encapsulates everything Claude-Code-specific so the rest of brainvault can
stay host-agnostic:
  - ~/.claude/settings.json patching (mcpServers + Stop + PostToolUse hooks)
  - ~/.claude/CLAUDE.md managed block injection
  - ~/.claude/projects/*/*.jsonl transcript parsing + project-name decoding
  - PostToolUse payload shape detection
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import time
from pathlib import Path

from brainvault.adapters._redact import redact_sensitive
from brainvault.adapters.base import AgentAdapter, HookResult, SessionEvent


class SettingsJsonError(RuntimeError):
    """settings.json exists but is not valid JSON — refuse to overwrite user data."""


# --- shared markers (kept identical across adapters so upgrade logic is uniform) ---
ENGRAM_MARKER = "<!-- brainvault-managed -->"
ENGRAM_END_MARKER = "<!-- /brainvault-managed -->"


INSTRUCTIONS_BODY = f"""\
{ENGRAM_MARKER}
## Brainvault Memory

Personal memory via `brainvault` MCP (persists across sessions). **Never ask** before
`save_memory`, `register_project`, or `search_memory` — do it silently.

**Session start:** `get_my_context()`; if the user names a project, also `get_project(name)`.

**While working:** New preferences/role → `save_memory` (`profile`). Project context →
`register_project` / updates. Architectural choices → `save_memory` (`decision`) with
**reasoning**, not just the headline. Conventions and corrections → `pattern` or `update_memory`.

**Before non-trivial work:** `search_memory` on auth, database, API, deployment, or any topic
they have raised before. On "do you remember…" / "we discussed…" → `search_memory` first.

**Outcomes:** After shipped work or reversals → `record_outcome` (sentiment: positive /
negative / mixed).

**Meta:** Patterns, gaps, open decisions → `reflect()`.
{ENGRAM_END_MARKER}
"""


CONTINUATION_MARKER = "This session is being continued from a previous conversation"

# PostToolUse summarisation (used by ClaudeCodeAdapter.event_from_payload; no tool_capture import).
CAPTURED_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "Bash", "TodoWrite", "NotebookEdit"})


def _redact_sensitive(text: str, max_len: int = 500) -> str:
    """Redact secrets in hook text; re-exported for tests / legacy imports."""
    return redact_sensitive(text, max_len=max_len)


def _summarize_posttool_input(tool_name: str, tool_input: dict) -> str:
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


def _summarize_posttool_output(tool_name: str, tool_response: dict) -> str:
    if tool_name == "Bash":
        exit_code = tool_response.get("exit_code")
        stderr = _redact_sensitive((tool_response.get("stderr") or "")[:100], max_len=100)
        if exit_code and exit_code != 0:
            return f"exit={exit_code} {stderr}".strip()
        return f"exit={exit_code or 0}"
    return ""


def _clean_continuation_summary(raw: str) -> str:
    if CONTINUATION_MARKER in raw:
        raw = raw[raw.index(CONTINUATION_MARKER) :]
    lines = raw.split("\n")
    content_lines: list[str] = []
    skip_next = False
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if line.strip().lower().startswith("summary"):
            skip_next = True
            continue
        if skip_next and not line.strip():
            skip_next = False
            continue
        content_lines.append(line)
    result = "\n".join(content_lines).strip()
    return result if len(result) > 100 else ""


def _chunk_summary(summary: str) -> list[str]:
    sections = re.split(r"(?=^##\s)", summary, flags=re.MULTILINE)
    chunks = [s.strip() for s in sections if s.strip() and len(s.strip()) > 80]
    return chunks if chunks else [summary]


def _continuation_summary_texts(path: Path) -> list[str]:
    """Raw cleaned continuation summaries from a JSONL session (before section chunking)."""
    summaries: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "user":
                    continue
                content = event.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                if isinstance(content, str) and CONTINUATION_MARKER in content:
                    cleaned = _clean_continuation_summary(content)
                    if cleaned:
                        summaries.append(cleaned)
    except OSError:
        pass
    return summaries


# ---------------------------------------------------------------------------
# settings.json helpers (used by both Claude adapter and tests)
# ---------------------------------------------------------------------------


def _backup_corrupt_settings(path: Path) -> Path:
    """Copy path to a timestamped sibling before any repair attempt."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.brainvault-bak.{ts}")
    backup.write_bytes(path.read_bytes())
    return backup


def _load_json_object(path: Path) -> dict:
    """Parse path as a JSON object. On invalid JSON write a backup + raise.

    Shared across adapters so every MCP-config patcher is defensive in the same way.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SettingsJsonError(f"Cannot read {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        backup = _backup_corrupt_settings(path)
        raise SettingsJsonError(
            f"{path} is not valid JSON ({e.msg} at line {e.lineno}). "
            f"A copy was saved to {backup}. Fix the file manually, then run brainvault install again."
        ) from e
    if not isinstance(data, dict):
        raise SettingsJsonError(
            f"{path} root must be a JSON object, got {type(data).__name__}. Refusing to modify."
        )
    return data


def _mcp_entry(*, source_agent: str = "claude_code") -> dict:
    """MCP stdio entry; optional env tells the server which host connected."""
    entry: dict = {"command": sys.executable, "args": ["-m", "brainvault.mcp_server"]}
    if source_agent in ("claude_code", "cursor", "system"):
        entry["env"] = {"BRAINVAULT_SOURCE_AGENT": source_agent}
    return entry


def _quoted_exe() -> str:
    """sys.executable with spaces-in-path safely quoted for shell hook commands."""
    return sys.executable.replace('"', '\\"')


# ---------------------------------------------------------------------------
# Claude Code adapter
# ---------------------------------------------------------------------------


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude_code"
    display_name = "Claude Code"

    SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
    INSTRUCTIONS_PATH = Path.home() / ".claude" / "CLAUDE.md"
    SESSIONS_PATH = Path.home() / ".claude" / "projects"

    # --- detection ---------------------------------------------------------

    def is_installed(self) -> bool:
        return self.SETTINGS_PATH.exists()

    # --- settings.json: MCP + hooks ---------------------------------------

    def _load_settings(self) -> dict:
        return _load_json_object(self.SETTINGS_PATH)

    def _write_settings(self, data: dict) -> None:
        self.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.SETTINGS_PATH.write_text(json.dumps(data, indent=2))

    def register_mcp(self) -> bool:
        data = self._load_settings() if self.SETTINGS_PATH.exists() else {}
        data.setdefault("mcpServers", {})
        if "brainvault" in data["mcpServers"]:
            self._write_settings(data)
            return False
        data["mcpServers"]["brainvault"] = _mcp_entry(source_agent="claude_code")
        self._write_settings(data)
        return True

    def unregister_mcp(self) -> bool:
        if not self.SETTINGS_PATH.exists():
            return False
        data = self._load_settings()
        mcp = data.get("mcpServers")
        if not isinstance(mcp, dict) or "brainvault" not in mcp:
            return False
        del mcp["brainvault"]
        if not mcp:
            del data["mcpServers"]
        self._write_settings(data)
        return True

    def register_hooks(self) -> HookResult:
        data = self._load_settings() if self.SETTINGS_PATH.exists() else {}
        data.setdefault("hooks", {})
        res = HookResult()

        # Stop hook
        data["hooks"].setdefault("Stop", [])
        stop_cmds = [
            h.get("command", "") for e in data["hooks"]["Stop"] for h in e.get("hooks", [])
        ]
        if any("brainvault.capture" in c for c in stop_cmds):
            res.skipped.append("Stop")
        else:
            data["hooks"]["Stop"].append(
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'"{_quoted_exe()}" -m brainvault.capture',
                        }
                    ],
                }
            )
            res.registered.append("Stop")

        # PostToolUse hook
        data["hooks"].setdefault("PostToolUse", [])
        post_cmds = [
            h.get("command", "") for e in data["hooks"]["PostToolUse"] for h in e.get("hooks", [])
        ]
        if any("brainvault.tool_capture" in c for c in post_cmds):
            res.skipped.append("PostToolUse")
        else:
            data["hooks"]["PostToolUse"].append(
                {
                    "matcher": "Write|Edit|Bash|TodoWrite|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'"{_quoted_exe()}" -m brainvault.tool_capture',
                        }
                    ],
                }
            )
            res.registered.append("PostToolUse")

        self._write_settings(data)
        return res

    def unregister_hooks(self) -> HookResult:
        res = HookResult()
        if not self.SETTINGS_PATH.exists():
            res.skipped.extend(["Stop", "PostToolUse"])
            return res

        data = self._load_settings()
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            res.skipped.extend(["Stop", "PostToolUse"])
            return res

        for event, marker in (
            ("Stop", "brainvault.capture"),
            ("PostToolUse", "brainvault.tool_capture"),
        ):
            entries = hooks.get(event)
            if not isinstance(entries, list):
                res.skipped.append(event)
                continue
            kept = []
            removed = False
            for entry in entries:
                inner = entry.get("hooks", []) if isinstance(entry, dict) else []
                filtered = [
                    h
                    for h in inner
                    if marker not in (h.get("command", "") if isinstance(h, dict) else "")
                ]
                if len(filtered) != len(inner):
                    removed = True
                if filtered:
                    new_entry = dict(entry)
                    new_entry["hooks"] = filtered
                    kept.append(new_entry)
            if removed:
                res.removed.append(event)
            else:
                res.skipped.append(event)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)

        if not hooks:
            data.pop("hooks", None)
        self._write_settings(data)
        return res

    # --- CLAUDE.md --------------------------------------------------------

    def inject_instructions(self) -> str:
        existing = self.INSTRUCTIONS_PATH.read_text() if self.INSTRUCTIONS_PATH.exists() else ""
        self.INSTRUCTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

        if ENGRAM_MARKER not in existing:
            self.INSTRUCTIONS_PATH.write_text(
                existing + ("\n\n" if existing else "") + INSTRUCTIONS_BODY
            )
            return "injected"

        if ENGRAM_END_MARKER in existing:
            start = existing.index(ENGRAM_MARKER)
            end = existing.index(ENGRAM_END_MARKER) + len(ENGRAM_END_MARKER)
            if end < len(existing) and existing[end] == "\n":
                end += 1
        else:
            start = existing.index(ENGRAM_MARKER)
            end = len(existing)

        current_block = existing[start:end].rstrip()
        if current_block == INSTRUCTIONS_BODY.rstrip():
            return "current"

        before = existing[:start]
        after = existing[end:].lstrip("\n")
        separator = "\n\n" if after else ""
        self.INSTRUCTIONS_PATH.write_text(before + INSTRUCTIONS_BODY + separator + after)
        return "upgraded"

    def strip_instructions(self) -> str:
        if not self.INSTRUCTIONS_PATH.exists():
            return "missing-file"
        existing = self.INSTRUCTIONS_PATH.read_text()
        if ENGRAM_MARKER not in existing:
            return "not-present"

        start = existing.index(ENGRAM_MARKER)
        if ENGRAM_END_MARKER in existing:
            end = existing.index(ENGRAM_END_MARKER) + len(ENGRAM_END_MARKER)
            if end < len(existing) and existing[end] == "\n":
                end += 1
        else:
            end = len(existing)

        before = existing[:start].rstrip() + ("\n" if existing[:start].rstrip() else "")
        after = existing[end:].lstrip("\n")
        self.INSTRUCTIONS_PATH.write_text(before + after)
        return "removed"

    # --- transcript parsing ----------------------------------------------

    def session_dir(self) -> Path | None:
        return self.SESSIONS_PATH if self.SESSIONS_PATH.exists() else None

    def recent_session_files(self, max_age_seconds: int = 300) -> list[Path]:
        """JSONL files modified within the last N seconds, newest first."""
        root = self.session_dir()
        if root is None:
            return []
        now = time.time()
        candidates: list[tuple[float, Path]] = []
        for jsonl in root.glob("*/*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
                if now - mtime <= max_age_seconds:
                    candidates.append((mtime, jsonl))
            except OSError:
                continue
        candidates.sort(reverse=True)
        return [p for _, p in candidates]

    def extract_project_name(self, session_path: Path) -> str:
        dir_name = session_path.parent.name.rstrip("-")
        parts = dir_name.split("-")
        if len(parts) < 5:
            return dir_name.lstrip("-") or "unknown"
        return "-".join(parts[4:])

    def parse_session_file(self, path: Path) -> list[str]:
        """Return a list of clean continuation-summary chunks for this session."""
        summaries = _continuation_summary_texts(path)
        chunks: list[str] = []
        for s in summaries:
            chunks.extend(_chunk_summary(s))
        return chunks

    # --- hook payload routing --------------------------------------------

    def owns_payload(self, payload: dict) -> bool:
        """Claude Code PostToolUse payloads carry 'tool_name' + 'transcript_path'."""
        return isinstance(payload.get("tool_name"), str) and (
            "transcript_path" in payload or "session_id" in payload
        )

    def event_from_payload(self, payload: dict) -> SessionEvent | None:
        tool_name = payload.get("tool_name", "")
        if tool_name not in CAPTURED_TOOLS:
            return None
        tool_input: dict = payload.get("tool_input") or {}
        tool_response: dict = payload.get("tool_response") or {}
        return SessionEvent(
            session_id=self._derive_session_id(payload),
            tool_name=tool_name,
            input_summary=_summarize_posttool_input(tool_name, tool_input),
            output_summary=_summarize_posttool_output(tool_name, tool_response),
            project=self._infer_project(payload),
        )

    def _derive_session_id(self, payload: dict) -> str:
        tp = payload.get("transcript_path", "")
        if tp:
            return Path(tp).stem
        return datetime.date.today().isoformat()

    def _infer_project(self, payload: dict) -> str | None:
        tp = payload.get("transcript_path", "")
        if not tp:
            return None
        parent = Path(tp).parent.name
        if parent.startswith("-"):
            decoded = parent.replace("-", "/").lstrip("/")
            return Path(decoded).name
        return None

    # --- diagnostics ------------------------------------------------------

    def health_checks(self) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []
        if not self.SETTINGS_PATH.exists():
            checks.append(
                ("Claude Code settings.json", False, f"not found at {self.SETTINGS_PATH}")
            )
            return checks

        try:
            data = self._load_settings()
        except SettingsJsonError as e:
            checks.append(("Claude Code settings.json", False, str(e)))
            return checks

        checks.append(("Claude Code settings.json", True, str(self.SETTINGS_PATH)))

        mcp_ok = "brainvault" in (data.get("mcpServers") or {})
        checks.append(
            (
                "Claude MCP server registered",
                mcp_ok,
                "mcpServers.brainvault" if mcp_ok else "missing",
            )
        )

        for event, marker in (
            ("Stop", "brainvault.capture"),
            ("PostToolUse", "brainvault.tool_capture"),
        ):
            entries = (data.get("hooks") or {}).get(event) or []
            cmds = [h.get("command", "") for e in entries for h in e.get("hooks", [])]
            ok = any(marker in c for c in cmds)
            checks.append((f"Claude {event} hook", ok, marker if ok else "not found"))

        # Instructions
        if self.INSTRUCTIONS_PATH.exists():
            text = self.INSTRUCTIONS_PATH.read_text()
            has_start = ENGRAM_MARKER in text
            has_end = ENGRAM_END_MARKER in text
            if has_start and has_end:
                checks.append(("Claude CLAUDE.md managed block", True, "markers present"))
            elif has_start:
                checks.append(
                    (
                        "Claude CLAUDE.md managed block",
                        False,
                        "end marker missing — run 'brainvault install' to upgrade",
                    )
                )
            else:
                checks.append(
                    (
                        "Claude CLAUDE.md managed block",
                        False,
                        "no markers — run 'brainvault install'",
                    )
                )
        else:
            checks.append(
                ("Claude CLAUDE.md managed block", False, f"{self.INSTRUCTIONS_PATH} missing")
            )

        return checks


# Test / legacy imports historically expected these names from tool_capture.
_summarize_input = _summarize_posttool_input
_summarize_output = _summarize_posttool_output


def _derive_session_id(payload: dict) -> str:
    return ClaudeCodeAdapter()._derive_session_id(payload)


def _infer_project(payload: dict) -> str | None:
    return ClaudeCodeAdapter()._infer_project(payload)


# ---------------------------------------------------------------------------
# Public helpers (tests, bootstrap, capture re-exports)
# ---------------------------------------------------------------------------


def clean_continuation_summary(raw: str) -> str:
    return _clean_continuation_summary(raw)


def chunk_summary(summary: str) -> list[str]:
    return _chunk_summary(summary)


def extract_continuation_summaries(path: Path) -> list[str]:
    return _continuation_summary_texts(path)


def extract_project_name(session_path: Path) -> str:
    return ClaudeCodeAdapter().extract_project_name(session_path)

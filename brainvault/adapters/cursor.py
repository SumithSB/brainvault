"""
brainvault/adapters/cursor.py — Cursor adapter.

Cursor host integration:
  - ~/.cursor/mcp.json — MCP server registration (same JSON shape as Claude settings.json)
  - ~/.cursor/rules/brainvault.mdc — always-on rules file with the shared
    instructions body, wrapped in frontmatter and the shared managed markers.
  - ~/.cursor/hooks.json — user-level agent hooks (stop, postToolUse, afterFileEdit,
    afterShellExecution) calling brainvault.capture / brainvault.tool_capture
  - ~/.cursor/projects/*/agent-transcripts/*/*.jsonl — session transcripts for capture
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from brainvault.adapters._redact import redact_sensitive
from brainvault.adapters.base import AgentAdapter, HookResult, SessionEvent, _HomeRelativePath
from brainvault.adapters.claude_code import (
    ENGRAM_END_MARKER,
    ENGRAM_MARKER,
    INSTRUCTIONS_BODY,
    SettingsJsonError,
    _load_json_object,
    _mcp_entry,
    _mcp_entry_equivalent,
    _quoted_exe,
)

_RULES_FRONTMATTER = """\
---
description: Brainvault personal memory — managed by brainvault install.
alwaysApply: true
---

"""

# Cursor postToolUse matcher: avoid dupes with afterFileEdit / afterShellExecution.
_POSTTOOL_MATCHER = r"Read|Grep|Task|Delete|MCP:.*"

# Inserted before ENGRAM_END_MARKER so it stays inside the managed block (upgrade-safe).
_CURSOR_MANAGED_NOTE = """
**Cursor host:** Brainvault MCP tools (`get_my_context`, `search_memory`, …) only run when this session **can call MCP** (e.g. **Agent** / tool-using agent — UI labels vary by Cursor version). Plain chat with tools disabled cannot invoke MCP even if rules load. Confirm **brainvault** is enabled for this chat in MCP settings. For **old** Cursor transcripts on disk, run `brainvault bootstrap --host cursor` once — the Stop hook only picks **recent** JSONL by modification time.
"""

_CURSOR_HOOK_EVENTS: tuple[tuple[str, str, str | None], ...] = (
    ("stop", "brainvault.capture", None),
    ("postToolUse", "brainvault.tool_capture", _POSTTOOL_MATCHER),
    ("afterFileEdit", "brainvault.tool_capture", None),
    ("afterShellExecution", "brainvault.tool_capture", None),
)


def iter_cursor_transcript_paths_under(root: Path) -> Iterator[Path]:
    """Yield every ``*.jsonl`` under ``root/*/agent-transcripts/*/*.jsonl``, oldest first."""
    files: list[tuple[float, Path]] = []
    for jsonl in root.glob("*/agent-transcripts/*/*.jsonl"):
        try:
            files.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue
    files.sort()
    for _, p in files:
        yield p


def _cursor_managed_body() -> str:
    """Shared instructions plus Cursor-specific paragraph, still inside managed markers."""
    if ENGRAM_END_MARKER not in INSTRUCTIONS_BODY:
        return INSTRUCTIONS_BODY
    return INSTRUCTIONS_BODY.replace(
        ENGRAM_END_MARKER,
        _CURSOR_MANAGED_NOTE.strip() + "\n\n" + ENGRAM_END_MARKER,
        1,
    )


def decode_workspace_slug(slug: str) -> str:
    """Decode a Cursor ``~/.cursor/projects/<slug>/`` folder name into a project identifier.

    Strips the leading ``Users-<username>-<container>-`` prefix so paths like
    ``Users-sumithsb-UoL-Dissertation-ssb49`` become ``Dissertation-ssb49``.
    """
    slug = slug.strip()
    if not slug:
        return "unknown"
    if not slug.startswith("Users-"):
        return slug.lstrip("-") or "unknown"
    parts = slug.split("-")
    if len(parts) < 4:
        return slug.lstrip("-") or "unknown"
    rest = "-".join(parts[3:])
    return rest or "unknown"


def _cursor_hook_command(module: str) -> dict:
    """Single Cursor hooks.json entry (command type, quoted interpreter)."""
    return {
        "type": "command",
        "command": f'"{_quoted_exe()}" -m brainvault.{module}',
    }


def _workspace_project_from_roots(payload: dict) -> str | None:
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list) or not roots:
        return None
    first = roots[0]
    if not isinstance(first, str) or not first:
        return None
    return Path(first).name or None


def _summarize_cursor_posttool_input(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Write":
        return f"→ {tool_input.get('file_path', tool_input.get('path', '?'))}"
    if tool_name == "Shell":
        return redact_sensitive(str(tool_input.get("command", "") or ""), max_len=300)
    return redact_sensitive(str(tool_input), max_len=200)


def _summarize_cursor_posttool_output(tool_name: str, tool_output: str | None) -> str:
    if not tool_output:
        return ""
    if tool_name == "Shell":
        try:
            parsed = json.loads(tool_output)
        except (json.JSONDecodeError, TypeError):
            return redact_sensitive(tool_output[:120], max_len=120)
        if isinstance(parsed, dict):
            code = parsed.get("exitCode", parsed.get("exit_code", ""))
            tail = redact_sensitive(str(parsed.get("stdout", "") or "")[:80], max_len=80)
            return f"exit={code} {tail}".strip()
    return redact_sensitive(tool_output[:200], max_len=200)


class CursorAdapter(AgentAdapter):
    name = "cursor"
    display_name = "Cursor"

    CURSOR_DIR = _HomeRelativePath(".cursor")
    MCP_CONFIG = _HomeRelativePath(".cursor", "mcp.json")
    RULES_DIR = _HomeRelativePath(".cursor", "rules")
    RULES_FILE = _HomeRelativePath(".cursor", "rules", "brainvault.mdc")
    HOOKS_CONFIG = _HomeRelativePath(".cursor", "hooks.json")
    SESSIONS_PATH = _HomeRelativePath(".cursor", "projects")

    # --- detection --------------------------------------------------------

    def is_installed(self) -> bool:
        if not self.CURSOR_DIR.exists():
            return False
        markers = ("mcp.json", "extensions", "settings.json", "User", "rules")
        return any((self.CURSOR_DIR / m).exists() for m in markers)

    # --- mcp.json ---------------------------------------------------------

    def _load_mcp_config(self) -> dict:
        return _load_json_object(self.MCP_CONFIG)

    def _write_mcp_config(self, data: dict) -> None:
        self.MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        self.MCP_CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def register_mcp(self) -> str:
        data = self._load_mcp_config() if self.MCP_CONFIG.exists() else {}
        data.setdefault("mcpServers", {})
        canonical = _mcp_entry(source_agent="cursor")
        existing = data["mcpServers"].get("brainvault")
        if existing is not None and _mcp_entry_equivalent(existing, canonical):
            return "skipped"
        data["mcpServers"]["brainvault"] = canonical
        self._write_mcp_config(data)
        return "registered" if existing is None else "updated"

    def configured_mcp_command(self) -> str | None:
        if not self.MCP_CONFIG.exists():
            return None
        try:
            data = self._load_mcp_config()
        except SettingsJsonError:
            return None
        return (data.get("mcpServers") or {}).get("brainvault", {}).get("command")

    def unregister_mcp(self) -> bool:
        if not self.MCP_CONFIG.exists():
            return False
        data = self._load_mcp_config()
        mcp = data.get("mcpServers")
        if not isinstance(mcp, dict) or "brainvault" not in mcp:
            return False
        del mcp["brainvault"]
        if not mcp:
            del data["mcpServers"]
        self._write_mcp_config(data)
        return True

    # --- hooks.json -------------------------------------------------------

    def _load_hooks(self) -> dict:
        if not self.HOOKS_CONFIG.exists():
            return {}
        return _load_json_object(self.HOOKS_CONFIG)

    def _write_hooks(self, data: dict) -> None:
        self.HOOKS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        self.HOOKS_CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _hooks_list_has_marker(self, entries: object, marker: str) -> bool:
        if not isinstance(entries, list):
            return False
        for item in entries:
            if not isinstance(item, dict):
                continue
            cmd = item.get("command", "")
            if isinstance(cmd, str) and marker in cmd:
                return True
        return False

    def register_hooks(self) -> HookResult:
        data = self._load_hooks() if self.HOOKS_CONFIG.exists() else {}
        data.setdefault("version", 1)
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            hooks = {}
            data["hooks"] = hooks
        res = HookResult()
        event_label = {
            "stop": "Stop",
            "postToolUse": "PostToolUse",
            "afterFileEdit": "AfterFileEdit",
            "afterShellExecution": "AfterShellExecution",
        }

        for event, marker, matcher in _CURSOR_HOOK_EVENTS:
            module = "capture" if marker == "brainvault.capture" else "tool_capture"
            entry = _cursor_hook_command(module)
            if matcher is not None:
                entry["matcher"] = matcher
            lst = hooks.setdefault(event, [])
            if not isinstance(lst, list):
                lst = []
                hooks[event] = lst
            if self._hooks_list_has_marker(lst, marker):
                res.skipped.append(event_label[event])
                continue
            lst.append(entry)
            res.registered.append(event_label[event])

        self._write_hooks(data)
        return res

    def unregister_hooks(self) -> HookResult:
        res = HookResult()
        if not self.HOOKS_CONFIG.exists():
            res.skipped.extend(["Stop", "PostToolUse", "AfterFileEdit", "AfterShellExecution"])
            return res

        data = self._load_hooks()
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            res.skipped.extend(["Stop", "PostToolUse", "AfterFileEdit", "AfterShellExecution"])
            return res

        label_by_event = {
            "stop": "Stop",
            "postToolUse": "PostToolUse",
            "afterFileEdit": "AfterFileEdit",
            "afterShellExecution": "AfterShellExecution",
        }
        marker_by_event = {
            "stop": "brainvault.capture",
            "postToolUse": "brainvault.tool_capture",
            "afterFileEdit": "brainvault.tool_capture",
            "afterShellExecution": "brainvault.tool_capture",
        }

        for event, label in label_by_event.items():
            entries = hooks.get(event)
            if not isinstance(entries, list):
                res.skipped.append(label)
                continue
            marker = marker_by_event[event]
            kept = [
                e
                for e in entries
                if not (
                    isinstance(e, dict)
                    and isinstance(e.get("command"), str)
                    and marker in e["command"]
                )
            ]
            if len(kept) < len(entries):
                res.removed.append(label)
            else:
                res.skipped.append(label)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)

        if not hooks:
            data.pop("hooks", None)
        self._write_hooks(data)
        return res

    # --- rules file (.mdc) ------------------------------------------------

    def _rules_body(self) -> str:
        return _RULES_FRONTMATTER + _cursor_managed_body()

    def inject_instructions(self) -> str:
        self.RULES_DIR.mkdir(parents=True, exist_ok=True)
        desired = self._rules_body()

        if not self.RULES_FILE.exists():
            self.RULES_FILE.write_text(desired, encoding="utf-8")
            return "injected"

        existing = self.RULES_FILE.read_text(encoding="utf-8")
        if existing == desired:
            return "current"

        if ENGRAM_MARKER in existing and ENGRAM_END_MARKER in existing:
            start = existing.index(ENGRAM_MARKER)
            end = existing.index(ENGRAM_END_MARKER) + len(ENGRAM_END_MARKER)
            if end < len(existing) and existing[end] == "\n":
                end += 1
            before = existing[:start]
            after = existing[end:].lstrip("\n")
            new_block = _cursor_managed_body()
            separator = "\n\n" if after else ""
            self.RULES_FILE.write_text(before + new_block + separator + after, encoding="utf-8")
            return "upgraded"

        self.RULES_FILE.write_text(desired, encoding="utf-8")
        return "upgraded"

    def strip_instructions(self) -> str:
        if not self.RULES_FILE.exists():
            return "missing-file"
        existing = self.RULES_FILE.read_text(encoding="utf-8")
        if ENGRAM_MARKER not in existing:
            return "not-present"
        self.RULES_FILE.unlink()
        try:
            self.RULES_DIR.rmdir()
        except OSError:
            pass
        return "removed"

    # --- transcripts ------------------------------------------------------

    def session_dir(self) -> Path | None:
        return self.SESSIONS_PATH if self.SESSIONS_PATH.exists() else None

    def recent_session_files(self, max_age_seconds: int = 300) -> list[Path]:
        root = self.session_dir()
        if root is None:
            return []
        now = time.time()
        candidates: list[tuple[float, Path]] = []
        for jsonl in root.glob("*/agent-transcripts/*/*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
                if now - mtime <= max_age_seconds:
                    candidates.append((mtime, jsonl))
            except OSError:
                continue
        candidates.sort(reverse=True)
        return [p for _, p in candidates]

    def iter_all_session_files(self) -> Iterator[Path]:
        """All agent JSONL transcripts under ~/.cursor/projects/, oldest first."""
        root = self.session_dir()
        if root is None:
            yield from ()
            return
        yield from iter_cursor_transcript_paths_under(root)

    def extract_project_name(self, session_path: Path) -> str:
        try:
            workspace_dir = session_path.parents[2].name
        except IndexError:
            return session_path.parent.name or "unknown"
        return decode_workspace_slug(workspace_dir)

    def parse_session_file(self, path: Path) -> list[str]:
        queries: list[str] = []
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
                    if event.get("role") != "user":
                        continue
                    text = _extract_cursor_user_text(event.get("message", {}))
                    text = text.strip()
                    if len(text) >= 20:
                        queries.append(text)
        except OSError:
            return []

        if not queries:
            return []
        combined = "\n".join(f"- {q}" for q in queries)
        if len(combined) < 100:
            return []
        sid = path.stem
        return [f"User queries in Cursor session {sid}:\n{combined}"]

    # --- hook payload routing ---------------------------------------------

    def owns_payload(self, payload: dict) -> bool:
        ev = payload.get("hook_event_name")
        if ev in ("postToolUse", "afterFileEdit", "afterShellExecution"):
            return True
        return False

    def event_from_payload(self, payload: dict) -> SessionEvent | None:
        ev = payload.get("hook_event_name")
        session_id = str(payload.get("conversation_id") or payload.get("session_id") or "")
        project = _workspace_project_from_roots(payload)

        if ev == "postToolUse":
            tool_name = str(payload.get("tool_name") or "")
            if tool_name in ("Write", "Shell"):
                return None
            tool_input = (
                payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
            )
            out_raw = payload.get("tool_output")
            out_str = out_raw if isinstance(out_raw, str) else ""
            return SessionEvent(
                session_id=session_id or "unknown",
                tool_name=tool_name or "?",
                input_summary=_summarize_cursor_posttool_input(tool_name, tool_input),
                output_summary=_summarize_cursor_posttool_output(tool_name, out_str),
                project=project,
            )

        if ev == "afterFileEdit":
            fp = str(payload.get("file_path") or "?")
            edits = payload.get("edits")
            n = len(edits) if isinstance(edits, list) else 0
            return SessionEvent(
                session_id=session_id or "unknown",
                tool_name="Write",
                input_summary=f"→ {fp}",
                output_summary=f"{n} edits" if n else "",
                project=project,
            )

        if ev == "afterShellExecution":
            cmd = str(payload.get("command") or "")
            duration = payload.get("duration")
            output = str(payload.get("output") or "")
            dur_ms = f"{duration}ms" if duration is not None else ""
            out_tail = redact_sensitive(output[-200:], max_len=200) if output else ""
            summary = dur_ms + (f" {out_tail}" if out_tail else "")
            return SessionEvent(
                session_id=session_id or "unknown",
                tool_name="Shell",
                input_summary=redact_sensitive(cmd, max_len=300),
                output_summary=summary.strip(),
                project=project,
            )

        return None

    # --- diagnostics ------------------------------------------------------

    def health_checks(self) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []
        if not self.CURSOR_DIR.exists():
            checks.append(("Cursor installed", False, f"{self.CURSOR_DIR} not found"))
            return checks
        checks.append(("Cursor installed", True, str(self.CURSOR_DIR)))

        if not self.MCP_CONFIG.exists():
            checks.append(("Cursor mcp.json", False, f"{self.MCP_CONFIG} missing"))
        else:
            try:
                data = self._load_mcp_config()
            except SettingsJsonError as e:
                checks.append(("Cursor mcp.json", False, str(e)))
            else:
                checks.append(("Cursor mcp.json", True, str(self.MCP_CONFIG)))
                mcp_ok = "brainvault" in (data.get("mcpServers") or {})
                checks.append(
                    (
                        "Cursor MCP server registered",
                        mcp_ok,
                        "mcpServers.brainvault" if mcp_ok else "missing",
                    )
                )

                if mcp_ok:
                    import subprocess as _subprocess

                    cmd = (data.get("mcpServers") or {}).get("brainvault", {}).get("command", "")
                    cmd_exists = bool(cmd) and Path(cmd).is_file()
                    checks.append(
                        (
                            "Cursor MCP command path exists",
                            cmd_exists,
                            cmd
                            if cmd_exists
                            else f"{cmd or '(empty)'} not found — run 'brainvault install' to repair",
                        )
                    )
                    if cmd_exists:
                        try:
                            r = _subprocess.run(
                                [cmd, "-c", "import brainvault.mcp_server"],
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            importable = r.returncode == 0
                            detail = (
                                cmd
                                if importable
                                else (r.stderr.strip().splitlines() or ["unknown error"])[-1]
                            )
                            checks.append(
                                ("Cursor MCP python can import brainvault", importable, detail)
                            )
                        except Exception as exc:
                            checks.append(
                                ("Cursor MCP python can import brainvault", False, str(exc))
                            )

        if self.RULES_FILE.exists():
            text = self.RULES_FILE.read_text(encoding="utf-8")
            has_start = ENGRAM_MARKER in text
            has_end = ENGRAM_END_MARKER in text
            if has_start and has_end:
                checks.append(("Cursor rules file", True, str(self.RULES_FILE)))
            else:
                checks.append(
                    (
                        "Cursor rules file",
                        False,
                        "markers missing — run 'brainvault install'",
                    )
                )
        else:
            checks.append(("Cursor rules file", False, f"{self.RULES_FILE} missing"))

        if self.SESSIONS_PATH.exists():
            checks.append(("Cursor projects / transcripts root", True, str(self.SESSIONS_PATH)))
        else:
            checks.append(
                (
                    "Cursor projects / transcripts root",
                    False,
                    f"{self.SESSIONS_PATH} missing (transcripts appear after agent use)",
                )
            )

        if not self.HOOKS_CONFIG.exists():
            checks.append(("Cursor hooks.json", False, f"{self.HOOKS_CONFIG} missing"))
        else:
            try:
                hdata = self._load_hooks()
            except SettingsJsonError as e:
                checks.append(("Cursor hooks.json", False, str(e)))
            else:
                checks.append(("Cursor hooks.json", True, str(self.HOOKS_CONFIG)))
                hooks = hdata.get("hooks") or {}
                if isinstance(hooks, dict):
                    for event, marker, _matcher in _CURSOR_HOOK_EVENTS:
                        label = {
                            "stop": "Cursor stop hook",
                            "postToolUse": "Cursor postToolUse hook",
                            "afterFileEdit": "Cursor afterFileEdit hook",
                            "afterShellExecution": "Cursor afterShellExecution hook",
                        }[event]
                        lst = hooks.get(event)
                        ok = self._hooks_list_has_marker(lst, marker)
                        checks.append((label, ok, marker if ok else "not found"))

        return checks


def _extract_cursor_user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        t = block.get("text", "")
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)

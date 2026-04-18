"""
tests/test_adapters.py — AgentAdapter registry + concrete adapter behaviour.

Every test monkey-patches the adapter's filesystem paths onto a tmp dir so the
user's real ~/.claude and ~/.cursor are never touched.
"""

from __future__ import annotations

import json

import pytest

from brainvault.adapters import (
    ALL_ADAPTERS,
    AgentAdapter,
    ClaudeCodeAdapter,
    CursorAdapter,
    all_adapters,
    installed_adapters,
    resolve,
)
from brainvault.adapters.claude_code import (
    ENGRAM_END_MARKER,
    ENGRAM_MARKER,
    INSTRUCTIONS_BODY,
    SettingsJsonError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_paths(tmp_path, monkeypatch):
    """Redirect ClaudeCodeAdapter filesystem paths into tmp_path."""
    settings = tmp_path / "claude" / "settings.json"
    md = tmp_path / "claude" / "CLAUDE.md"
    sessions = tmp_path / "claude" / "projects"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}")
    monkeypatch.setattr(ClaudeCodeAdapter, "SETTINGS_PATH", settings)
    monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
    monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", sessions)
    return {"settings": settings, "md": md, "sessions": sessions}


@pytest.fixture
def cursor_paths(tmp_path, monkeypatch):
    """Redirect CursorAdapter filesystem paths into tmp_path."""
    cursor_dir = tmp_path / "cursor"
    cursor_dir.mkdir(parents=True)
    mcp = cursor_dir / "mcp.json"
    rules_dir = cursor_dir / "rules"
    rules_file = rules_dir / "brainvault.mdc"
    hooks = cursor_dir / "hooks.json"
    projects = cursor_dir / "projects"
    monkeypatch.setattr(CursorAdapter, "CURSOR_DIR", cursor_dir)
    monkeypatch.setattr(CursorAdapter, "MCP_CONFIG", mcp)
    monkeypatch.setattr(CursorAdapter, "RULES_DIR", rules_dir)
    monkeypatch.setattr(CursorAdapter, "RULES_FILE", rules_file)
    monkeypatch.setattr(CursorAdapter, "HOOKS_CONFIG", hooks)
    monkeypatch.setattr(CursorAdapter, "SESSIONS_PATH", projects)
    return {
        "dir": cursor_dir,
        "mcp": mcp,
        "rules": rules_file,
        "hooks": hooks,
        "projects": projects,
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_all_adapters_enumerated(self):
        names = {cls.name for cls in ALL_ADAPTERS}
        assert names == {"claude_code", "cursor"}

    def test_all_adapters_instantiates_each(self):
        instances = all_adapters()
        assert len(instances) == len(ALL_ADAPTERS)
        assert all(isinstance(a, AgentAdapter) for a in instances)

    def test_installed_adapters_filters(self, monkeypatch):
        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: False)
        names = {a.name for a in installed_adapters()}
        assert names == {"claude_code"}

    def test_resolve_auto_returns_installed(self, monkeypatch):
        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: False)
        assert {a.name for a in resolve(None)} == {"claude_code"}
        assert {a.name for a in resolve(["auto"])} == {"claude_code"}

    def test_resolve_all_ignores_detection(self, monkeypatch):
        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: False)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: False)
        assert {a.name for a in resolve(["all"])} == {"claude_code", "cursor"}

    def test_resolve_named(self):
        adapters = resolve(["cursor"])
        assert [a.name for a in adapters] == ["cursor"]

    def test_resolve_friendly_alias(self):
        adapters = resolve(["claude"])
        assert [a.name for a in adapters] == ["claude_code"]

    def test_resolve_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown agent"):
            resolve(["windsurf"])


# ---------------------------------------------------------------------------
# Claude Code adapter — round-trip via the adapter interface
# ---------------------------------------------------------------------------


class TestClaudeCodeAdapter:
    def test_is_installed_reflects_settings_file(self, claude_paths):
        adapter = ClaudeCodeAdapter()
        assert adapter.is_installed() is True
        claude_paths["settings"].unlink()
        assert adapter.is_installed() is False

    def test_register_mcp_adds_entry(self, claude_paths):
        adapter = ClaudeCodeAdapter()
        assert adapter.register_mcp() is True
        data = json.loads(claude_paths["settings"].read_text())
        assert "brainvault" in data["mcpServers"]
        assert data["mcpServers"]["brainvault"]["env"]["BRAINVAULT_SOURCE_AGENT"] == "claude_code"

    def test_register_mcp_is_idempotent(self, claude_paths):
        adapter = ClaudeCodeAdapter()
        adapter.register_mcp()
        assert adapter.register_mcp() is False

    def test_register_hooks_adds_both(self, claude_paths):
        adapter = ClaudeCodeAdapter()
        res = adapter.register_hooks()
        assert set(res.registered) == {"Stop", "PostToolUse"}

    def test_register_hooks_skips_existing(self, claude_paths):
        adapter = ClaudeCodeAdapter()
        adapter.register_hooks()
        res = adapter.register_hooks()
        assert set(res.skipped) == {"Stop", "PostToolUse"}
        assert not res.registered

    def test_inject_and_strip_instructions(self, claude_paths):
        adapter = ClaudeCodeAdapter()
        assert adapter.inject_instructions() == "injected"
        assert ENGRAM_MARKER in claude_paths["md"].read_text()
        assert adapter.inject_instructions() == "current"
        assert adapter.strip_instructions() == "removed"
        assert ENGRAM_MARKER not in claude_paths["md"].read_text()

    def test_unregister_mcp_and_hooks_preserves_unrelated(self, claude_paths):
        claude_paths["settings"].write_text(
            json.dumps(
                {
                    "mcpServers": {"other": {"command": "x", "args": []}},
                    "hooks": {
                        "Stop": [
                            {
                                "matcher": "",
                                "hooks": [{"type": "command", "command": "echo keep-me"}],
                            }
                        ]
                    },
                }
            )
        )
        adapter = ClaudeCodeAdapter()
        adapter.register_mcp()
        adapter.register_hooks()

        assert adapter.unregister_mcp() is True
        res = adapter.unregister_hooks()
        assert "Stop" in res.removed

        after = json.loads(claude_paths["settings"].read_text())
        assert "other" in after["mcpServers"]
        stop_cmds = [
            h.get("command", "") for entry in after["hooks"]["Stop"] for h in entry.get("hooks", [])
        ]
        assert any("keep-me" in c for c in stop_cmds)
        assert not any("brainvault.capture" in c for c in stop_cmds)

    def test_invalid_settings_json_raises(self, claude_paths):
        claude_paths["settings"].write_text('{"not": closed')
        with pytest.raises(SettingsJsonError):
            ClaudeCodeAdapter().register_mcp()

    def test_health_checks_no_settings(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.json"
        monkeypatch.setattr(ClaudeCodeAdapter, "SETTINGS_PATH", missing)
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", tmp_path / "CLAUDE.md")
        rows = ClaudeCodeAdapter().health_checks()
        assert rows[0][1] is False
        assert str(missing) in rows[0][2]

    def test_health_checks_after_install(self, claude_paths):
        adapter = ClaudeCodeAdapter()
        adapter.register_mcp()
        adapter.register_hooks()
        adapter.inject_instructions()
        rows = adapter.health_checks()
        assert all(ok for _, ok, _ in rows), rows


# ---------------------------------------------------------------------------
# Cursor adapter
# ---------------------------------------------------------------------------


class TestCursorAdapter:
    def test_is_installed_requires_cursor_dir(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope"
        monkeypatch.setattr(CursorAdapter, "CURSOR_DIR", missing)
        assert CursorAdapter().is_installed() is False

    def test_is_installed_empty_dir_returns_false(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty_cursor"
        empty.mkdir()
        monkeypatch.setattr(CursorAdapter, "CURSOR_DIR", empty)
        assert CursorAdapter().is_installed() is False

    def test_is_installed_true_when_extensions_present(self, tmp_path, monkeypatch):
        d = tmp_path / "with_marker"
        d.mkdir()
        (d / "extensions").mkdir()
        monkeypatch.setattr(CursorAdapter, "CURSOR_DIR", d)
        assert CursorAdapter().is_installed() is True

    def test_register_mcp_creates_file(self, cursor_paths):
        adapter = CursorAdapter()
        assert adapter.register_mcp() is True
        data = json.loads(cursor_paths["mcp"].read_text())
        assert data["mcpServers"]["brainvault"]["args"] == ["-m", "brainvault.mcp_server"]
        assert data["mcpServers"]["brainvault"]["env"]["BRAINVAULT_SOURCE_AGENT"] == "cursor"

    def test_register_mcp_is_idempotent(self, cursor_paths):
        adapter = CursorAdapter()
        adapter.register_mcp()
        assert adapter.register_mcp() is False

    def test_register_mcp_preserves_unrelated(self, cursor_paths):
        cursor_paths["mcp"].write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"})
        )
        CursorAdapter().register_mcp()
        data = json.loads(cursor_paths["mcp"].read_text())
        assert data["theme"] == "dark"
        assert "other" in data["mcpServers"]
        assert "brainvault" in data["mcpServers"]

    def test_unregister_mcp_removes_and_keeps_others(self, cursor_paths):
        cursor_paths["mcp"].write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        CursorAdapter().register_mcp()
        assert CursorAdapter().unregister_mcp() is True
        data = json.loads(cursor_paths["mcp"].read_text())
        assert "other" in data["mcpServers"]
        assert "brainvault" not in data["mcpServers"]

    def test_unregister_mcp_absent_returns_false(self, cursor_paths):
        # No mcp.json yet
        assert CursorAdapter().unregister_mcp() is False

    def test_inject_instructions_creates_mdc(self, cursor_paths):
        adapter = CursorAdapter()
        assert adapter.inject_instructions() == "injected"
        text = cursor_paths["rules"].read_text()
        assert "alwaysApply: true" in text
        assert ENGRAM_MARKER in text
        assert ENGRAM_END_MARKER in text
        # Idempotent
        assert adapter.inject_instructions() == "current"

    def test_inject_instructions_upgrades_block_in_place(self, cursor_paths):
        cursor_paths["rules"].parent.mkdir(parents=True, exist_ok=True)
        header = "---\nalwaysApply: true\n---\n\n# user preamble\n\n"
        old_block = f"{ENGRAM_MARKER}\n## Brainvault Memory\nOld stuff.\n{ENGRAM_END_MARKER}\n"
        trailer = "\n# user epilogue\n"
        cursor_paths["rules"].write_text(header + old_block + trailer)
        assert CursorAdapter().inject_instructions() == "upgraded"
        text = cursor_paths["rules"].read_text()
        assert "# user preamble" in text
        assert "# user epilogue" in text
        assert "Old stuff." not in text
        assert INSTRUCTIONS_BODY.rstrip() in text

    def test_strip_instructions_deletes_file(self, cursor_paths):
        adapter = CursorAdapter()
        adapter.inject_instructions()
        assert cursor_paths["rules"].exists()
        assert adapter.strip_instructions() == "removed"
        assert not cursor_paths["rules"].exists()

    def test_strip_instructions_leaves_foreign_file(self, cursor_paths):
        cursor_paths["rules"].parent.mkdir(parents=True, exist_ok=True)
        cursor_paths["rules"].write_text("# not managed by brainvault\n")
        assert CursorAdapter().strip_instructions() == "not-present"
        assert cursor_paths["rules"].exists()

    def test_register_hooks_writes_hooks_json(self, cursor_paths):
        adapter = CursorAdapter()
        res = adapter.register_hooks()
        assert set(res.registered) == {
            "Stop",
            "PostToolUse",
            "AfterFileEdit",
            "AfterShellExecution",
        }
        data = json.loads(cursor_paths["hooks"].read_text())
        assert data.get("version") == 1
        hooks = data["hooks"]
        stop_cmds = [e.get("command", "") for e in hooks.get("stop", []) if isinstance(e, dict)]
        assert any("brainvault.capture" in c for c in stop_cmds)
        pt = hooks.get("postToolUse", [])
        assert any(
            "brainvault.tool_capture" in e.get("command", "") for e in pt if isinstance(e, dict)
        )
        assert any(
            e.get("matcher") == r"Read|Grep|Task|Delete|MCP:.*" for e in pt if isinstance(e, dict)
        )
        for key in ("afterFileEdit", "afterShellExecution"):
            lst = hooks.get(key, [])
            assert any(
                "brainvault.tool_capture" in e.get("command", "")
                for e in lst
                if isinstance(e, dict)
            )

    def test_register_hooks_idempotent(self, cursor_paths):
        adapter = CursorAdapter()
        adapter.register_hooks()
        res = adapter.register_hooks()
        assert set(res.skipped) == {"Stop", "PostToolUse", "AfterFileEdit", "AfterShellExecution"}
        assert not res.registered

    def test_unregister_hooks_removes_brainvault_preserves_user(self, cursor_paths):
        cursor_paths["hooks"].write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {
                        "stop": [
                            {"type": "command", "command": "echo user-stop"},
                            {
                                "type": "command",
                                "command": f'"{__import__("sys").executable}" -m brainvault.capture',
                            },
                        ],
                        "postToolUse": [
                            {
                                "type": "command",
                                "command": f'"{__import__("sys").executable}" -m brainvault.tool_capture',
                                "matcher": r"Read|Grep|Task|Delete|MCP:.*",
                            }
                        ],
                        "afterFileEdit": [
                            {
                                "type": "command",
                                "command": f'"{__import__("sys").executable}" -m brainvault.tool_capture',
                            }
                        ],
                        "afterShellExecution": [
                            {
                                "type": "command",
                                "command": f'"{__import__("sys").executable}" -m brainvault.tool_capture',
                            }
                        ],
                    },
                }
            )
        )
        adapter = CursorAdapter()
        res = adapter.unregister_hooks()
        assert set(res.removed) == {"Stop", "PostToolUse", "AfterFileEdit", "AfterShellExecution"}
        data = json.loads(cursor_paths["hooks"].read_text())
        stop_cmds = [e.get("command", "") for e in data["hooks"]["stop"] if isinstance(e, dict)]
        assert any("user-stop" in c for c in stop_cmds)
        assert not any("brainvault.capture" in c for c in stop_cmds)
        assert "postToolUse" not in data["hooks"] or not any(
            "brainvault" in e.get("command", "") for e in data["hooks"].get("postToolUse", [])
        )

    def test_cursor_owns_payload_three_events(self):
        adapter = CursorAdapter()
        assert adapter.owns_payload({"hook_event_name": "postToolUse", "tool_name": "Read"})
        assert adapter.owns_payload({"hook_event_name": "afterFileEdit", "file_path": "/x"})
        assert adapter.owns_payload({"hook_event_name": "afterShellExecution", "command": "ls"})
        assert not adapter.owns_payload(
            {"tool_name": "Write", "tool_input": {}, "transcript_path": "/.claude/projects/x.jsonl"}
        )

    def test_cursor_event_from_payload_posttooluse(self):
        adapter = CursorAdapter()
        ev = adapter.event_from_payload(
            {
                "hook_event_name": "postToolUse",
                "conversation_id": "conv-1",
                "workspace_roots": ["/Users/me/Projects/foo"],
                "tool_name": "Read",
                "tool_input": {"path": "/a.py"},
                "tool_output": "{}",
            }
        )
        assert ev is not None
        assert ev.session_id == "conv-1"
        assert ev.tool_name == "Read"
        assert ev.project == "foo"

    def test_cursor_event_from_payload_posttooluse_drops_write_shell(self):
        adapter = CursorAdapter()
        assert (
            adapter.event_from_payload(
                {
                    "hook_event_name": "postToolUse",
                    "conversation_id": "c",
                    "tool_name": "Write",
                    "tool_input": {},
                }
            )
            is None
        )

    def test_cursor_event_from_payload_after_file_edit(self):
        adapter = CursorAdapter()
        ev = adapter.event_from_payload(
            {
                "hook_event_name": "afterFileEdit",
                "conversation_id": "c2",
                "workspace_roots": ["/w"],
                "file_path": "/w/src/x.ts",
                "edits": [{"old_string": "a", "new_string": "b"}],
            }
        )
        assert ev is not None
        assert ev.tool_name == "Write"
        assert "x.ts" in ev.input_summary
        assert "1 edits" in ev.output_summary

    def test_cursor_event_from_payload_after_shell_execution(self):
        adapter = CursorAdapter()
        ev = adapter.event_from_payload(
            {
                "hook_event_name": "afterShellExecution",
                "conversation_id": "c3",
                "command": "pytest -q",
                "duration": 1200,
                "output": "3 passed\n",
            }
        )
        assert ev is not None
        assert ev.tool_name == "Shell"
        assert "pytest" in ev.input_summary
        assert "1200ms" in ev.output_summary

    def test_cursor_parse_session_file_extracts_user_queries(self, tmp_path):
        sid = "11111111-1111-1111-1111-111111111111"
        transcript = (
            tmp_path
            / "cursor"
            / "projects"
            / "Users-me-Projects-demo"
            / "agent-transcripts"
            / sid
            / f"{sid}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        long_q = "Review this codebase for security issues and performance. " * 4
        transcript.write_text(
            json.dumps(
                {
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": long_q}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        adapter = CursorAdapter()
        chunks = adapter.parse_session_file(transcript)
        assert len(chunks) == 1
        assert "User queries in Cursor session" in chunks[0]
        assert "security" in chunks[0]

    def test_cursor_extract_project_name(self, tmp_path):
        sid = "22222222-2222-2222-2222-222222222222"
        p = (
            tmp_path
            / "projects"
            / "Users-sumithsb-Projects-brainvault"
            / "agent-transcripts"
            / sid
            / f"{sid}.jsonl"
        )
        adapter = CursorAdapter()
        assert adapter.extract_project_name(p) == "brainvault"

    def test_health_checks_after_install(self, cursor_paths):
        adapter = CursorAdapter()
        adapter.register_mcp()
        adapter.register_hooks()
        adapter.inject_instructions()
        cursor_paths["projects"].mkdir(parents=True)
        rows = adapter.health_checks()
        assert all(ok for _, ok, _ in rows), rows


# ---------------------------------------------------------------------------
# DB source_agent column — writes + reads
# ---------------------------------------------------------------------------


class TestSourceAgentColumn:
    def test_save_memory_default_is_claude_code(self, tmp_db):
        from brainvault import db

        mid = db.save_memory("test", "note")
        with db.get_connection() as conn:
            row = conn.execute("SELECT source_agent FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row["source_agent"] == "claude_code"

    def test_save_memory_custom_source_agent(self, tmp_db):
        from brainvault import db

        mid = db.save_memory("hi", "note", source_agent="cursor")
        with db.get_connection() as conn:
            row = conn.execute("SELECT source_agent FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row["source_agent"] == "cursor"

    def test_record_tool_event_tagged(self, tmp_db):
        from brainvault import db

        db.record_tool_event(
            session_id="s1",
            tool_name="Write",
            input_summary="→ a.py",
            project="proj",
            source_agent="cursor",
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT source_agent FROM session_events WHERE session_id = ?", ("s1",)
            ).fetchone()
        assert row["source_agent"] == "cursor"

    def test_mark_session_captured_tagged(self, tmp_db):
        from brainvault import db

        db.mark_session_captured("/tmp/x.jsonl", memory_count=3, source_agent="cursor")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT source_agent FROM sessions_captured WHERE session_path = ?",
                ("/tmp/x.jsonl",),
            ).fetchone()
        assert row["source_agent"] == "cursor"

    def test_save_memory_invalid_source_agent_raises(self, tmp_db):
        from brainvault import db

        with pytest.raises(ValueError, match="Invalid source_agent"):
            db.save_memory("x", "note", source_agent="bogus")

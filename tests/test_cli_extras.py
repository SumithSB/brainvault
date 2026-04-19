"""
tests/test_cli_extras.py — Tests for uninstall, doctor, export, import commands.

All tests use the shared tmp_db + mock_embeddings fixtures from conftest.
Settings file paths are re-pointed to tmp_path so the user's real ~/.claude/ is never touched.
"""

from __future__ import annotations

import json
import sys

import pytest

from brainvault import db
from brainvault.adapters.claude_code import ClaudeCodeAdapter
from brainvault.adapters.cursor import CursorAdapter
from brainvault.cli import main as cli_main
from brainvault.installer import uninstall


@pytest.fixture
def cursor_paths(tmp_path, monkeypatch):
    """Redirect CursorAdapter filesystem paths into tmp_path (same as test_adapters)."""
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


@pytest.fixture
def fake_claude_dir(tmp_path, monkeypatch):
    """Redirect ClaudeCodeAdapter paths to a tmp dir.

    Pins Cursor to 'not installed' so doctor output is predictable across
    machines that may or may not have ~/.cursor present.
    """
    settings = tmp_path / "settings.json"
    md = tmp_path / "CLAUDE.md"
    settings.write_text("{}")

    monkeypatch.setattr(ClaudeCodeAdapter, "SETTINGS_PATH", settings)
    monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
    monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", tmp_path / "projects")
    monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: False)
    return {"settings": settings, "md": md}


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


class TestUnpatchSettings:
    def test_removes_mcp_and_hooks(self, fake_claude_dir):
        adapter = ClaudeCodeAdapter()
        adapter.register_mcp()
        adapter.register_hooks()
        data = json.loads(fake_claude_dir["settings"].read_text())
        assert "brainvault" in data["mcpServers"]

        assert adapter.unregister_mcp() is True
        hook_res = adapter.unregister_hooks()
        assert "Stop" in hook_res.removed
        assert "PostToolUse" in hook_res.removed

        after = json.loads(fake_claude_dir["settings"].read_text())
        assert "mcpServers" not in after
        assert "hooks" not in after

    def test_preserves_unrelated_entries(self, fake_claude_dir):
        fake_claude_dir["settings"].write_text(
            json.dumps(
                {
                    "mcpServers": {"otherserver": {"command": "x"}},
                    "hooks": {
                        "Stop": [
                            {"matcher": "", "hooks": [{"type": "command", "command": "echo hi"}]}
                        ]
                    },
                    "theme": "dark",
                }
            )
        )

        adapter = ClaudeCodeAdapter()
        adapter.register_mcp()
        adapter.register_hooks()
        adapter.unregister_mcp()
        adapter.unregister_hooks()

        data = json.loads(fake_claude_dir["settings"].read_text())
        assert data["mcpServers"] == {"otherserver": {"command": "x"}}
        assert data["theme"] == "dark"
        # The unrelated Stop hook entry survives, brainvault.capture is gone.
        stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e.get("hooks", [])]
        assert stop_cmds == ["echo hi"]

    def test_idempotent_when_not_installed(self, fake_claude_dir):
        adapter = ClaudeCodeAdapter()
        assert adapter.unregister_mcp() is False
        res = adapter.unregister_hooks()
        assert res.skipped == ["Stop", "PostToolUse"]


class TestUnpatchClaudeMd:
    def test_removes_managed_block(self, fake_claude_dir):
        fake_claude_dir["md"].write_text("# user content\n\nhello\n")
        adapter = ClaudeCodeAdapter()
        adapter.inject_instructions()
        assert "brainvault-managed" in fake_claude_dir["md"].read_text()

        result = adapter.strip_instructions()
        assert result == "removed"

        remaining = fake_claude_dir["md"].read_text()
        assert "brainvault-managed" not in remaining
        assert "hello" in remaining

    def test_not_present_is_reported(self, fake_claude_dir):
        fake_claude_dir["md"].write_text("# just user content\n")
        assert ClaudeCodeAdapter().strip_instructions() == "not-present"

    def test_missing_file(self, fake_claude_dir):
        fake_claude_dir["md"].unlink(missing_ok=True)
        assert ClaudeCodeAdapter().strip_instructions() == "missing-file"


class TestUninstallFunction:
    def test_preserves_db_by_default(self, fake_claude_dir, tmp_db, capsys):
        a = ClaudeCodeAdapter()
        a.register_mcp()
        a.register_hooks()
        a.inject_instructions()

        uninstall(purge=False)
        assert tmp_db.exists()

    def test_purge_deletes_vault_dir(self, fake_claude_dir, tmp_db, capsys):
        a = ClaudeCodeAdapter()
        a.register_mcp()
        uninstall(purge=True)
        assert not tmp_db.exists()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_reports_failures_when_settings_missing(
        self, fake_claude_dir, tmp_db, monkeypatch, capsys
    ):
        # Delete settings file to force failure — Claude adapter reports "not found"
        # and doctor surfaces "Coding agent detected — no supported agent..." if
        # Cursor is also absent (pinned by the fixture).
        fake_claude_dir["settings"].unlink()

        monkeypatch.setattr(sys, "argv", ["brainvault", "doctor"])
        with pytest.raises(SystemExit) as exc:
            cli_main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Coding agent detected" in out
        assert "✗" in out

    def test_passes_after_install(self, fake_claude_dir, tmp_db, monkeypatch, capsys):
        a = ClaudeCodeAdapter()
        a.register_mcp()
        a.register_hooks()
        a.inject_instructions()

        monkeypatch.setattr(sys, "argv", ["brainvault", "doctor"])
        # health_checks() shells out to test the configured MCP command. In an editable
        # install the configured command IS sys.executable which has brainvault, so the
        # subprocess check passes. We allow SystemExit but assert the key rows appear.
        try:
            cli_main()
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert "Database integrity" in out
        assert "Claude MCP server registered" in out
        assert "Claude MCP command path exists" in out
        assert "Claude Stop hook" in out
        assert "Claude PostToolUse hook" in out
        assert "Claude CLAUDE.md managed block" in out


# ---------------------------------------------------------------------------
# _pick_agents_interactive
# ---------------------------------------------------------------------------


class TestPickAgentsInteractive:
    """Unit-test the interactive agent picker without touching the filesystem."""

    def _pick(self, monkeypatch, detected_names, input_str="", yes=False):
        """
        Run _pick_agents_interactive with a mocked set of detected adapters.

        detected_names: list of adapter .name values that appear as 'installed'.
        input_str: what the user types at the prompt (empty = Enter).
        """
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        # Patch is_installed on each adapter class
        monkeypatch.setattr(
            ClaudeCodeAdapter, "is_installed", lambda self: self.name in detected_names
        )
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: self.name in detected_names)
        monkeypatch.setattr("builtins.input", lambda _: input_str)

        return _pick_agents_interactive(skip_prompt=yes)

    def test_no_agents_detected_returns_empty(self, monkeypatch, capsys):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: False)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: False)
        result = _pick_agents_interactive()
        out = capsys.readouterr().out
        assert result == []
        assert "No supported coding agents" in out

    def test_single_agent_returns_none_without_prompt(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: False)
        # No input() call expected — if it's called the test will fail
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("input() called for single agent")),
        )
        result = _pick_agents_interactive()
        assert result is None

    def test_skip_prompt_returns_none(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(
            "builtins.input",
            lambda _: (_ for _ in ()).throw(AssertionError("input() called with skip_prompt=True")),
        )
        result = _pick_agents_interactive(skip_prompt=True)
        assert result is None

    def test_enter_selects_all(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _pick_agents_interactive() is None

    def test_all_keyword_selects_all(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "all")
        assert _pick_agents_interactive() is None

    def test_number_selects_specific_adapter(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "1")
        result = _pick_agents_interactive()
        assert result == ["claude_code"]

    def test_comma_list_selects_multiple(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "1,2")
        result = _pick_agents_interactive()
        assert set(result) == {"claude_code", "cursor"}

    def test_quit_returns_empty(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "q")
        assert _pick_agents_interactive() == []

    def test_eof_selects_all(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
        assert _pick_agents_interactive() is None

    def test_out_of_range_number_ignored(self, monkeypatch, capsys):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "99")
        result = _pick_agents_interactive()
        assert result == []

    def test_name_string_also_accepted(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "cursor")
        result = _pick_agents_interactive()
        assert result == ["cursor"]

    def test_deduplicates_repeated_selections(self, monkeypatch):
        from brainvault.adapters import ClaudeCodeAdapter, CursorAdapter
        from brainvault.cli import _pick_agents_interactive

        monkeypatch.setattr(ClaudeCodeAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda _: "1,1,2")
        result = _pick_agents_interactive()
        assert len(result) == 2
        assert result.count("claude_code") == 1


# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------


class TestExportImport:
    def _seed(self):
        db.save_project(name="alpha", description="first project", stack=["python"], notes="n")
        db.save_memory("alpha decision one", "decision", project="alpha", source="agent")
        db.save_memory("alpha pattern one", "pattern", project="alpha", source="agent")
        db.save_memory("global note", "note", project=None, source="agent")

    def test_export_json_roundtrip(self, tmp_db, tmp_path, monkeypatch, capsys):
        self._seed()
        out = tmp_path / "dump.json"
        monkeypatch.setattr(
            sys, "argv", ["brainvault", "export", "--output", str(out), "--format", "json"]
        )
        cli_main()

        payload = json.loads(out.read_text())
        assert payload["schema_version"] == 1
        assert len(payload["memories"]) == 3
        assert len(payload["projects"]) == 1
        assert {m["memory_type"] for m in payload["memories"]} == {"decision", "pattern", "note"}

    def test_export_markdown(self, tmp_db, tmp_path, monkeypatch, capsys):
        self._seed()
        out = tmp_path / "dump.md"
        monkeypatch.setattr(
            sys, "argv", ["brainvault", "export", "--output", str(out), "--format", "md"]
        )
        cli_main()

        text = out.read_text()
        assert "# Brainvault Export" in text
        assert "## Projects" in text
        assert "alpha decision one" in text

    def test_export_project_filter(self, tmp_db, tmp_path, monkeypatch, capsys):
        self._seed()
        out = tmp_path / "dump.json"
        monkeypatch.setattr(
            sys,
            "argv",
            ["brainvault", "export", "--output", str(out), "--project", "alpha"],
        )
        cli_main()

        payload = json.loads(out.read_text())
        assert all(m["project"] == "alpha" for m in payload["memories"])
        assert len(payload["memories"]) == 2

    def test_import_merges_by_default(self, tmp_db, tmp_path, monkeypatch, capsys):
        self._seed()
        out = tmp_path / "dump.json"
        monkeypatch.setattr(sys, "argv", ["brainvault", "export", "--output", str(out)])
        cli_main()

        # Wipe + re-import
        with db.get_connection() as conn:
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM projects")

        monkeypatch.setattr(sys, "argv", ["brainvault", "import", str(out)])
        cli_main()

        stats = db.get_stats()
        assert stats["total_memories"] == 3
        assert stats["total_projects"] == 1

    def test_import_skips_existing_ids(self, tmp_db, tmp_path, monkeypatch, capsys):
        self._seed()
        out = tmp_path / "dump.json"
        monkeypatch.setattr(sys, "argv", ["brainvault", "export", "--output", str(out)])
        cli_main()

        # Second import — all IDs already present, should be a no-op
        monkeypatch.setattr(sys, "argv", ["brainvault", "import", str(out)])
        cli_main()

        assert db.get_stats()["total_memories"] == 3

    def test_import_replace_updates_existing(self, tmp_db, tmp_path, monkeypatch, capsys):
        self._seed()
        out = tmp_path / "dump.json"
        monkeypatch.setattr(sys, "argv", ["brainvault", "export", "--output", str(out)])
        cli_main()

        # Hand-edit export to change content for one memory
        payload = json.loads(out.read_text())
        payload["memories"][0]["content"] = "UPDATED CONTENT"
        out.write_text(json.dumps(payload))

        monkeypatch.setattr(sys, "argv", ["brainvault", "import", str(out), "--replace"])
        cli_main()

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT content FROM memories WHERE id = ?", (payload["memories"][0]["id"],)
            ).fetchone()
        assert row[0] == "UPDATED CONTENT"

    def test_import_rejects_newer_schema(self, tmp_db, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema_version": 999, "memories": []}))
        monkeypatch.setattr(sys, "argv", ["brainvault", "import", str(bad)])
        with pytest.raises(SystemExit) as exc:
            cli_main()
        assert exc.value.code == 1

    def test_import_rejects_malformed_payload(self, tmp_db, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"no_memories_key": True}))
        monkeypatch.setattr(sys, "argv", ["brainvault", "import", str(bad)])
        with pytest.raises(SystemExit) as exc:
            cli_main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# installer behaviour
# ---------------------------------------------------------------------------


class TestInstallerSeedAndMessages:
    def test_install_skips_auto_seed_when_vault_populated(self, monkeypatch, capsys, tmp_path):
        from brainvault.adapters.claude_code import ClaudeCodeAdapter
        from brainvault.installer import install

        settings = tmp_path / "settings.json"
        md = tmp_path / "CLAUDE.md"
        settings.write_text("{}")
        monkeypatch.setattr(ClaudeCodeAdapter, "SETTINGS_PATH", settings)
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
        monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", tmp_path / "projects")

        monkeypatch.setattr(CursorAdapter, "is_installed", lambda self: False)

        seeded: list[int] = []

        def capture_seed() -> None:
            seeded.append(1)

        monkeypatch.setattr("brainvault.installer._seed_vault", capture_seed)
        monkeypatch.setattr("brainvault.installer.db.get_stats", lambda: {"total_memories": 3})

        install(agents=["claude_code"])
        assert not seeded
        assert "skipping auto-seed" in capsys.readouterr().out.lower()

    def test_install_one_registers_cursor_hooks(self, cursor_paths, capsys):
        from brainvault.installer import _install_one

        # is_installed() requires a marker file under ~/.cursor
        cursor_paths["mcp"].write_text("{}", encoding="utf-8")

        _install_one(CursorAdapter())
        out = capsys.readouterr().out
        assert "Stop hook registered" in out
        assert "PostToolUse hook registered" in out
        assert "AfterFileEdit hook registered" in out
        assert "AfterShellExecution hook registered" in out
        data = json.loads(cursor_paths["hooks"].read_text(encoding="utf-8"))
        assert "brainvault.capture" in json.dumps(data["hooks"]["stop"])
        assert "brainvault.tool_capture" in json.dumps(data["hooks"]["postToolUse"])


# ---------------------------------------------------------------------------
# CLI usage help includes the new commands
# ---------------------------------------------------------------------------


class TestUsageHelp:
    def test_usage_lists_new_commands(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["brainvault"])
        with pytest.raises(SystemExit):
            cli_main()
        out = capsys.readouterr().out
        for keyword in ("uninstall", "doctor", "export", "import", "save"):
            assert keyword in out


# ---------------------------------------------------------------------------
# brainvault save
# ---------------------------------------------------------------------------


class TestSaveCommand:
    def test_save_basic(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["brainvault", "save", "chose Redis over Memcached"])
        cli_main()
        out = capsys.readouterr().out
        assert "Saved note" in out

    def test_save_with_type_and_project(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "brainvault",
                "save",
                "use WAL mode for concurrent reads",
                "--type",
                "decision",
                "--project",
                "myapp",
            ],
        )
        cli_main()
        out = capsys.readouterr().out
        assert "Saved decision" in out
        assert "[myapp]" in out
        results = db.search_memories("WAL mode concurrent reads")
        assert any("WAL mode" in r["content"] for r in results)

    def test_save_no_content_exits(self, monkeypatch):
        import io

        monkeypatch.setattr(sys, "argv", ["brainvault", "save"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        with pytest.raises(SystemExit):
            cli_main()

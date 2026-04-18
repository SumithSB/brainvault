"""
tests/test_new_features.py — Tests for status, update_memory, outcome_sentiment,
reflect CLI, forget CLI, installer CLAUDE.md patch logic.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from brainvault import db
from brainvault.adapters.claude_code import (
    ENGRAM_END_MARKER,
    ENGRAM_MARKER,
    INSTRUCTIONS_BODY,
    ClaudeCodeAdapter,
    SettingsJsonError,
)

# ---------------------------------------------------------------------------
# db.get_status()
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_empty_vault(self):
        s = db.get_status()
        assert s["total_memories"] == 0
        assert s["by_type"] == {}
        assert s["by_source"] == {}
        assert s["unembedded"] == 0
        assert s["git_repos"] == 0
        assert s["git_memories"] == 0
        assert s["last_session_at"] is None
        assert s["last_session_memories"] == 0
        assert s["open_decisions"] == 0
        assert s["stale_projects"] == 0

    def test_counts_by_type_and_source(self):
        db.save_memory("decision one", "decision", source="agent")
        db.save_memory("pattern one", "pattern", source="agent")
        db.save_memory("note from git", "note", source="git")
        s = db.get_status()
        assert s["total_memories"] == 3
        assert s["by_type"]["decision"] == 1
        assert s["by_type"]["pattern"] == 1
        assert s["by_type"]["note"] == 1
        assert s["by_source"]["agent"] == 2
        assert s["by_source"]["git"] == 1

    def test_git_memories_counted(self):
        db.save_memory("git commit memory", "note", source="git")
        db.mark_commit_scanned("/repo/path", "abc123")
        s = db.get_status()
        assert s["git_memories"] == 1
        assert s["git_repos"] == 1

    def test_open_decisions_only_older_than_7_days(self):
        # New decision should NOT appear in open_decisions count
        db.save_memory("fresh decision", "decision", source="agent")
        s = db.get_status()
        assert s["open_decisions"] == 0

    def test_stale_projects_counted(self):
        # Insert a project with last_active far in the past
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO projects (name, description, stack, status, last_active)
                VALUES ('oldproject', 'old', '[]', 'active', '2020-01-01')
                """
            )
        s = db.get_status()
        assert s["stale_projects"] == 1


# ---------------------------------------------------------------------------
# db.update_memory()
# ---------------------------------------------------------------------------


class TestUpdateMemory:
    def test_update_content(self):
        mid = db.save_memory("original content", "note")
        result = db.update_memory(mid, content="updated content")
        assert result is True
        with db.get_connection() as conn:
            row = conn.execute("SELECT content FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row["content"] == "updated content"

    def test_update_type(self):
        mid = db.save_memory("some decision", "note")
        db.update_memory(mid, memory_type="decision")
        with db.get_connection() as conn:
            row = conn.execute("SELECT memory_type FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row["memory_type"] == "decision"

    def test_update_project(self):
        db.save_project("myproject", "desc", [])
        mid = db.save_memory("memory", "note")
        db.update_memory(mid, project="myproject")
        with db.get_connection() as conn:
            row = conn.execute("SELECT project FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row["project"] == "myproject"

    def test_update_nonexistent_returns_false(self):
        result = db.update_memory("00000000-0000-0000-0000-000000000000", content="x")
        assert result is False

    def test_update_content_changes_keywords(self):
        mid = db.save_memory("authentication jwt token", "note")
        db.update_memory(mid, content="postgresql database migration")
        with db.get_connection() as conn:
            row = conn.execute("SELECT keywords FROM memories WHERE id = ?", (mid,)).fetchone()
        import json

        kws = json.loads(row["keywords"])
        # New keywords should reflect new content
        assert any(k in kws for k in ("postgresql", "database", "migration"))

    def test_update_content_replaces_vector(self):
        mid = db.save_memory("original", "note")
        initial_count = db.count_embedded()
        db.update_memory(mid, content="completely different text")
        # Vector should still exist (re-embedded)
        assert db.count_embedded() == initial_count

    def test_partial_update_preserves_other_fields(self):
        mid = db.save_memory("my content", "pattern", project=None)
        db.update_memory(mid, memory_type="decision")
        with db.get_connection() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row["content"] == "my content"
        assert row["memory_type"] == "decision"


# ---------------------------------------------------------------------------
# db.record_outcome() with sentiment
# ---------------------------------------------------------------------------


class TestRecordOutcomeSentiment:
    def test_positive_sentiment_stored(self):
        mid = db.save_memory("use FastAPI", "decision")
        db.record_outcome(mid, "worked great at scale", sentiment="positive")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome, outcome_sentiment FROM memories WHERE id = ?", (mid,)
            ).fetchone()
        assert row["outcome"] == "worked great at scale"
        assert row["outcome_sentiment"] == "positive"

    def test_negative_sentiment_stored(self):
        mid = db.save_memory("use redis for sessions", "decision")
        db.record_outcome(mid, "caused memory issues", sentiment="negative")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome_sentiment FROM memories WHERE id = ?", (mid,)
            ).fetchone()
        assert row["outcome_sentiment"] == "negative"

    def test_mixed_sentiment_stored(self):
        mid = db.save_memory("monorepo structure", "decision")
        db.record_outcome(mid, "worked for some teams, not all", sentiment="mixed")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome_sentiment FROM memories WHERE id = ?", (mid,)
            ).fetchone()
        assert row["outcome_sentiment"] == "mixed"

    def test_invalid_sentiment_silently_ignored(self):
        mid = db.save_memory("some choice", "decision")
        db.record_outcome(mid, "it happened", sentiment="terrible")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome_sentiment FROM memories WHERE id = ?", (mid,)
            ).fetchone()
        assert row["outcome_sentiment"] is None

    def test_no_sentiment_defaults_null(self):
        mid = db.save_memory("another choice", "decision")
        db.record_outcome(mid, "it worked")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome_sentiment FROM memories WHERE id = ?", (mid,)
            ).fetchone()
        assert row["outcome_sentiment"] is None

    def test_outcome_only_on_decision_type(self):
        mid = db.save_memory("a note", "note")
        result = db.record_outcome(mid, "some outcome", sentiment="positive")
        assert result is False

    def test_sentiment_summary_in_reflection_data(self):
        m1 = db.save_memory("decision a", "decision")
        m2 = db.save_memory("decision b", "decision")
        m3 = db.save_memory("decision c", "decision")
        # Age them past 7-day threshold for open_decisions, but sentiment works regardless
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE memories SET created_at = '2020-01-01' WHERE memory_type = 'decision'"
            )
        db.record_outcome(m1, "worked", sentiment="positive")
        db.record_outcome(m2, "failed", sentiment="negative")
        db.record_outcome(m3, "meh")
        data = db.get_reflection_data()
        summary = data["outcome_sentiment_summary"]
        assert summary.get("positive") == 1
        assert summary.get("negative") == 1
        assert summary.get("unrated") == 1


# ---------------------------------------------------------------------------
# CLI: _cmd_status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def _run(self, args=None):
        from brainvault.cli import _cmd_status

        with patch.object(sys, "argv", ["brainvault", "status"] + (args or [])):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                _cmd_status()
            return buf.getvalue()

    def test_shows_memory_count(self):
        db.save_memory("something", "note")
        out = self._run()
        assert "1" in out
        assert "Memories" in out

    def test_shows_by_type(self):
        db.save_memory("a decision", "decision")
        out = self._run()
        assert "decision" in out

    def test_shows_all_up_to_date_when_no_unembedded(self):
        out = self._run()
        assert "all up to date" in out


# ---------------------------------------------------------------------------
# CLI: _cmd_update
# ---------------------------------------------------------------------------


class TestCmdUpdate:
    def _run(self, args):
        with patch.object(sys, "argv", ["brainvault", "update"] + args):
            from brainvault.cli import _cmd_update

            _cmd_update()

    def test_update_content(self, capsys):
        mid = db.save_memory("old content", "note")
        self._run([mid, "--content", "new content"])
        out = capsys.readouterr().out
        assert "updated" in out.lower()
        with db.get_connection() as conn:
            row = conn.execute("SELECT content FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row["content"] == "new content"

    def test_no_args_exits(self):
        with patch.object(sys, "argv", ["brainvault", "update"]):
            from brainvault.cli import _cmd_update

            with pytest.raises(SystemExit):
                _cmd_update()

    def test_no_fields_exits(self):
        with patch.object(sys, "argv", ["brainvault", "update", "some-id"]):
            from brainvault.cli import _cmd_update

            with pytest.raises(SystemExit):
                _cmd_update()

    def test_invalid_type_exits(self):
        mid = db.save_memory("x", "note")
        with patch.object(sys, "argv", ["brainvault", "update", mid, "--type", "bogus"]):
            from brainvault.cli import _cmd_update

            with pytest.raises(SystemExit):
                _cmd_update()

    def test_not_found_exits(self):
        with patch.object(sys, "argv", ["brainvault", "update", "no-such-id", "--content", "x"]):
            from brainvault.cli import _cmd_update

            with pytest.raises(SystemExit):
                _cmd_update()


# ---------------------------------------------------------------------------
# CLI: _cmd_reflect
# ---------------------------------------------------------------------------


class TestCmdReflect:
    def _run(self):
        import io
        from contextlib import redirect_stdout

        from brainvault.cli import _cmd_reflect

        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_reflect()
        return buf.getvalue()

    def test_empty_vault_no_crash(self):
        out = self._run()
        assert "reflection" in out.lower()

    def test_shows_open_decisions(self):
        mid = db.save_memory("use postgres", "decision")
        # Age past 7 days
        with db.get_connection() as conn:
            conn.execute("UPDATE memories SET created_at = '2020-01-01' WHERE id = ?", (mid,))
        out = self._run()
        assert "use postgres" in out

    def test_shows_sentiment_summary(self):
        mid = db.save_memory("use redis", "decision")
        with db.get_connection() as conn:
            conn.execute("UPDATE memories SET created_at = '2020-01-01' WHERE id = ?", (mid,))
        db.record_outcome(mid, "worked well", sentiment="positive")
        out = self._run()
        assert "positive" in out


# ---------------------------------------------------------------------------
# CLI: _cmd_forget
# ---------------------------------------------------------------------------


class TestCmdForget:
    def test_deletes_existing_memory(self, capsys):
        mid = db.save_memory("to be deleted", "note")
        with patch.object(sys, "argv", ["brainvault", "forget", mid]):
            from brainvault.cli import _cmd_forget

            _cmd_forget()
        out = capsys.readouterr().out
        assert "deleted" in out.lower()
        with db.get_connection() as conn:
            row = conn.execute("SELECT id FROM memories WHERE id = ?", (mid,)).fetchone()
        assert row is None

    def test_not_found_exits(self):
        with patch.object(sys, "argv", ["brainvault", "forget", "no-such-id"]):
            from brainvault.cli import _cmd_forget

            with pytest.raises(SystemExit):
                _cmd_forget()

    def test_no_args_exits(self):
        with patch.object(sys, "argv", ["brainvault", "forget"]):
            from brainvault.cli import _cmd_forget

            with pytest.raises(SystemExit):
                _cmd_forget()


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.inject_instructions()
# ---------------------------------------------------------------------------


class TestPatchClaudeMd:
    def test_fresh_install_injects_snippet(self, tmp_path, monkeypatch):
        md = tmp_path / "CLAUDE.md"
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
        result = ClaudeCodeAdapter().inject_instructions()
        assert result == "injected"
        content = md.read_text()
        assert ENGRAM_MARKER in content
        assert ENGRAM_END_MARKER in content

    def test_fresh_install_with_existing_content_appends(self, tmp_path, monkeypatch):
        md = tmp_path / "CLAUDE.md"
        md.write_text("# My notes\n\nSome existing content.\n")
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
        result = ClaudeCodeAdapter().inject_instructions()
        assert result == "injected"
        content = md.read_text()
        assert "My notes" in content
        assert ENGRAM_MARKER in content

    def test_already_current_returns_current(self, tmp_path, monkeypatch):
        md = tmp_path / "CLAUDE.md"
        md.write_text(INSTRUCTIONS_BODY, encoding="utf-8")
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
        result = ClaudeCodeAdapter().inject_instructions()
        assert result == "current"

    def test_old_content_upgrades(self, tmp_path, monkeypatch):
        md = tmp_path / "CLAUDE.md"
        old_snippet = f"{ENGRAM_MARKER}\n## Brainvault Memory\n\nOld instructions here.\n"
        md.write_text(old_snippet)
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
        result = ClaudeCodeAdapter().inject_instructions()
        assert result == "upgraded"
        content = md.read_text()
        assert "Old instructions" not in content
        assert ENGRAM_END_MARKER in content

    def test_upgrade_preserves_content_before_marker(self, tmp_path, monkeypatch):
        md = tmp_path / "CLAUDE.md"
        before = "# My personal notes\n\nKeep this.\n\n"
        old_block = f"{ENGRAM_MARKER}\n## Brainvault Memory\nOld stuff.\n"
        md.write_text(before + old_block)
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
        result = ClaudeCodeAdapter().inject_instructions()
        assert result == "upgraded"
        content = md.read_text()
        assert "Keep this." in content
        assert "Old stuff." not in content

    def test_upgrade_with_end_marker_replaces_block(self, tmp_path, monkeypatch):
        md = tmp_path / "CLAUDE.md"
        old_block = f"{ENGRAM_MARKER}\n## Brainvault Memory\nOld stuff.\n{ENGRAM_END_MARKER}\n"
        after = "\n# My other notes\n"
        md.write_text(old_block + after)
        monkeypatch.setattr(ClaudeCodeAdapter, "INSTRUCTIONS_PATH", md)
        result = ClaudeCodeAdapter().inject_instructions()
        assert result == "upgraded"
        content = md.read_text()
        assert "Old stuff." not in content
        assert "My other notes" in content


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.register_mcp — invalid JSON must not wipe user config
# ---------------------------------------------------------------------------


class TestPatchClaudeSettings:
    def test_invalid_json_raises_and_preserves_file(self, tmp_path, monkeypatch):
        settings = tmp_path / "settings.json"
        bad = '{ "not": closed'
        settings.write_text(bad)
        monkeypatch.setattr(ClaudeCodeAdapter, "SETTINGS_PATH", settings)
        with pytest.raises(SettingsJsonError) as excinfo:
            ClaudeCodeAdapter().register_mcp()
        assert "not valid JSON" in str(excinfo.value)
        assert settings.read_text() == bad
        backups = list(tmp_path.glob("settings.json.brainvault-bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == bad

    def test_valid_json_preserves_unrelated_keys(self, tmp_path, monkeypatch):
        settings = tmp_path / "settings.json"
        original = {
            "permissions": {"allow": ["Bash"]},
            "mcpServers": {"other": {"command": "true", "args": []}},
            "hooks": {},
        }
        import json

        settings.write_text(json.dumps(original, indent=2))
        monkeypatch.setattr(ClaudeCodeAdapter, "SETTINGS_PATH", settings)
        adapter = ClaudeCodeAdapter()
        adapter.register_mcp()
        adapter.register_hooks()
        data = json.loads(settings.read_text())
        assert data["permissions"] == original["permissions"]
        assert "other" in data["mcpServers"]
        assert "brainvault" in data["mcpServers"]
        assert any(
            "brainvault.capture" in h.get("command", "")
            for entry in data["hooks"]["Stop"]
            for h in entry.get("hooks", [])
        )

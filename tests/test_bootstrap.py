"""
tests/test_bootstrap.py — Tests for brainvault/bootstrap.py
"""

import json
from pathlib import Path

from brainvault import db
from brainvault.adapters.claude_code import CONTINUATION_MARKER, ClaudeCodeAdapter
from brainvault.bootstrap import _extract_session_data, bootstrap


def _write_session(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


# --- _extract_session_data ---


def test_extract_session_data_gets_title(tmp_path):
    session_file = tmp_path / "session.jsonl"
    _write_session(
        session_file,
        [
            {"type": "ai-title", "aiTitle": "Fix flaky integration test in checkout flow"},
            {"type": "user", "message": {"role": "user", "content": "hello"}},
        ],
    )
    ai_title, summaries = _extract_session_data(session_file)
    assert ai_title == "Fix flaky integration test in checkout flow"
    assert summaries == []


def test_extract_session_data_gets_continuation_summary(tmp_path):
    body = "The user is building a FastAPI backend. JWT was chosen for stateless scaling. PostgreSQL is the DB. This is long enough to pass the minimum threshold."
    session_file = tmp_path / "session.jsonl"
    _write_session(
        session_file,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"{CONTINUATION_MARKER}\n{body}"}],
                },
            },
        ],
    )
    ai_title, summaries = _extract_session_data(session_file)
    assert ai_title is None
    assert len(summaries) == 1
    assert "FastAPI" in summaries[0]


def test_extract_session_data_both_present(tmp_path):
    body = "The user is building a FastAPI backend. JWT was chosen for stateless scaling. PostgreSQL is the DB. This is long enough to pass the minimum threshold."
    session_file = tmp_path / "session.jsonl"
    _write_session(
        session_file,
        [
            {"type": "ai-title", "aiTitle": "Bootstrap brainvault"},
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"{CONTINUATION_MARKER}\n{body}"}],
                },
            },
        ],
    )
    ai_title, summaries = _extract_session_data(session_file)
    assert ai_title == "Bootstrap brainvault"
    assert len(summaries) == 1


def test_extract_session_data_empty_file(tmp_path):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("")
    ai_title, summaries = _extract_session_data(session_file)
    assert ai_title is None
    assert summaries == []


# --- bootstrap ---


def test_bootstrap_saves_continuation_summary(tmp_path, monkeypatch):
    body = "The user is building a FastAPI backend. JWT was chosen for stateless scaling. PostgreSQL is the database. This is long enough to pass the minimum threshold."
    project_dir = tmp_path / "-Users-sumithsb-Projects-pluto"
    session_file = project_dir / "abc.jsonl"
    _write_session(
        session_file,
        [
            {"type": "ai-title", "aiTitle": "Build pluto API"},
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"{CONTINUATION_MARKER}\n{body}"}],
                },
            },
        ],
    )

    monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", tmp_path)
    stats = bootstrap(verbose=False, hosts=frozenset({"claude_code"}))

    assert stats["continuation_summaries"] == 1
    assert stats["ai_titles"] == 0  # suppressed when summary exists
    assert stats["total_memories"] == 1

    results = db.search_memories("FastAPI")
    assert len(results) == 1
    assert results[0]["source"] == "bootstrap"


def test_bootstrap_saves_ai_title_when_no_summary(tmp_path, monkeypatch):
    project_dir = tmp_path / "-Users-sumithsb-Projects-ivy"
    session_file = project_dir / "xyz.jsonl"
    _write_session(
        session_file,
        [
            {"type": "ai-title", "aiTitle": "Set up React frontend"},
            {"type": "user", "message": {"role": "user", "content": "hello"}},
        ],
    )

    monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", tmp_path)
    stats = bootstrap(verbose=False, hosts=frozenset({"claude_code"}))

    assert stats["ai_titles"] == 1
    assert stats["continuation_summaries"] == 0
    assert stats["total_memories"] == 1

    results = db.search_memories("React frontend")
    assert len(results) == 1
    assert "Session: Set up React frontend" in results[0]["content"]


def test_bootstrap_is_idempotent(tmp_path, monkeypatch):
    project_dir = tmp_path / "-Users-sumithsb-Projects-pluto"
    session_file = project_dir / "abc.jsonl"
    _write_session(
        session_file,
        [
            {"type": "ai-title", "aiTitle": "Fix bug"},
        ],
    )

    monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", tmp_path)

    first = bootstrap(verbose=False, hosts=frozenset({"claude_code"}))
    second = bootstrap(verbose=False, hosts=frozenset({"claude_code"}))

    assert first["total_memories"] == 1
    assert second["total_memories"] == 0
    assert second["sessions_skipped"] == 1


def test_bootstrap_skips_session_with_no_data(tmp_path, monkeypatch):
    project_dir = tmp_path / "-Users-sumithsb-Projects-pluto"
    session_file = project_dir / "empty.jsonl"
    _write_session(
        session_file,
        [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
        ],
    )

    monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", tmp_path)
    stats = bootstrap(verbose=False, hosts=frozenset({"claude_code"}))

    assert stats["total_memories"] == 0
    assert stats["sessions_scanned"] == 1


def test_bootstrap_missing_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeCodeAdapter, "SESSIONS_PATH", tmp_path / "nonexistent")
    stats = bootstrap(verbose=False, hosts=frozenset({"claude_code"}))
    assert stats["sessions_scanned"] == 0


def test_bootstrap_cursor_saves_transcript_memories(tmp_path, monkeypatch):
    """Cursor bulk path reuses capture.process_session (source=hook, source_agent=cursor)."""
    projects_root = tmp_path / "cursor_projects"
    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    workspace = projects_root / "Users-me-Projects-demo"
    transcript = workspace / "agent-transcripts" / sid / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    long_q = "Review this codebase for security issues and performance. " * 4
    transcript.write_text(
        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": long_q}]}})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("brainvault.bootstrap.cursor_projects_dir", lambda: projects_root)

    stats = bootstrap(verbose=False, hosts=frozenset({"cursor"}))
    assert stats["cursor_sessions_scanned"] == 1
    assert stats["cursor_memories_saved"] >= 1

    rows = db.search_memories("security")
    assert any("security" in r["content"].lower() for r in rows)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT source_agent, source FROM memories WHERE content LIKE '%security%' LIMIT 1"
        ).fetchone()
    assert row["source_agent"] == "cursor"
    assert row["source"] == "hook"

    second = bootstrap(verbose=False, hosts=frozenset({"cursor"}))
    assert second["cursor_sessions_skipped"] == 1
    assert second["cursor_memories_saved"] == 0

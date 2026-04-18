"""
tests/test_bootstrap.py — Tests for brainvault/bootstrap.py
"""

import json
from pathlib import Path

from brainvault import db
from brainvault.adapters.claude_code import CONTINUATION_MARKER
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
            {"type": "ai-title", "aiTitle": "Fix auth middleware bug"},
            {"type": "user", "message": {"role": "user", "content": "hello"}},
        ],
    )
    ai_title, summaries = _extract_session_data(session_file)
    assert ai_title == "Fix auth middleware bug"
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

    monkeypatch.setattr("brainvault.bootstrap.claude_projects_dir", lambda: tmp_path)
    stats = bootstrap(verbose=False)

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

    monkeypatch.setattr("brainvault.bootstrap.claude_projects_dir", lambda: tmp_path)
    stats = bootstrap(verbose=False)

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

    monkeypatch.setattr("brainvault.bootstrap.claude_projects_dir", lambda: tmp_path)

    first = bootstrap(verbose=False)
    second = bootstrap(verbose=False)

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

    monkeypatch.setattr("brainvault.bootstrap.claude_projects_dir", lambda: tmp_path)
    stats = bootstrap(verbose=False)

    assert stats["total_memories"] == 0
    assert stats["sessions_scanned"] == 1


def test_bootstrap_missing_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "brainvault.bootstrap.claude_projects_dir", lambda: tmp_path / "nonexistent"
    )
    stats = bootstrap(verbose=False)
    assert stats["sessions_scanned"] == 0

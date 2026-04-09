"""
tests/test_capture.py — Tests for brainvault/capture.py
"""

import json
from pathlib import Path

from brainvault import db
from brainvault.capture import (
    CONTINUATION_MARKER,
    chunk_summary,
    clean_continuation_summary,
    extract_continuation_summaries,
    extract_project_name,
    process_session,
)


def _write_session(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


# --- extract_project_name ---


def test_extract_project_name_single_word():
    path = Path("/Users/sumithsb/.claude/projects/-Users-sumithsb-Projects-pluto/session.jsonl")
    assert extract_project_name(path) == "pluto"


def test_extract_project_name_multi_word():
    path = Path(
        "/Users/sumithsb/.claude/projects/-Users-sumithsb-Projects-job-tracking-assistant/session.jsonl"
    )
    assert extract_project_name(path) == "job-tracking-assistant"


def test_extract_project_name_org_prefix():
    path = Path("/Users/sumithsb/.claude/projects/-Users-sumithsb-Visiminds-ivy-main/session.jsonl")
    assert extract_project_name(path) == "ivy-main"


def test_extract_project_name_camelcase():
    path = Path(
        "/Users/sumithsb/.claude/projects/-Users-sumithsb-Projects-InterviewAI/session.jsonl"
    )
    assert extract_project_name(path) == "InterviewAI"


# --- chunk_summary ---


def test_chunk_summary_splits_on_headings():
    section1 = (
        "## 1. Primary Request\n" + "User asked to build a FastAPI backend with PostgreSQL. " * 3
    )
    section2 = (
        "## 2. Technical Concepts\n"
        + "FastAPI with PostgreSQL and JWT auth for stateless scaling. " * 3
    )
    summary = section1 + "\n\n" + section2
    chunks = chunk_summary(summary)
    assert len(chunks) == 2
    assert "Primary Request" in chunks[0]
    assert "Technical Concepts" in chunks[1]


def test_chunk_summary_fallback_no_headings():
    summary = "A" * 150  # long enough, no headings
    chunks = chunk_summary(summary)
    assert len(chunks) == 1
    assert chunks[0] == summary


def test_chunk_summary_skips_short_sections():
    summary = "## 1. Title\nToo short.\n\n## 2. Real Content\n" + "A" * 100
    chunks = chunk_summary(summary)
    assert len(chunks) == 1
    assert "Real Content" in chunks[0]


# --- clean_continuation_summary ---


def test_clean_continuation_summary_strips_header():
    raw = f"{CONTINUATION_MARKER}\nSummary\n\nThe user is building a FastAPI backend. JWT auth was chosen for stateless scaling. PostgreSQL is the database. This is a longer summary that exceeds the minimum length threshold."
    result = clean_continuation_summary(raw)
    assert CONTINUATION_MARKER not in result
    assert "FastAPI" in result


def test_clean_continuation_summary_too_short_returns_empty():
    raw = f"{CONTINUATION_MARKER}\nShort."
    result = clean_continuation_summary(raw)
    assert result == ""


def test_clean_continuation_summary_with_noise_before_marker():
    noise = "<ide_context>some injected content</ide_context>"
    summary_body = "The user is building a FastAPI backend with PostgreSQL. JWT auth was chosen for stateless scaling. Redis is used for caching. This is a detailed summary that is definitely long enough."
    raw = f"{noise}{CONTINUATION_MARKER}\n{summary_body}"
    result = clean_continuation_summary(raw)
    assert "FastAPI" in result
    assert noise not in result


# --- extract_continuation_summaries ---


def test_extract_continuation_summaries_finds_summary(tmp_path):
    summary_body = "The user is building a FastAPI backend. JWT auth was chosen for stateless scaling. PostgreSQL is the database. This exceeds the minimum length for a valid summary."
    session_file = tmp_path / "session.jsonl"
    _write_session(
        session_file,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"{CONTINUATION_MARKER}\n{summary_body}"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Got it, continuing..."}],
                },
            },
        ],
    )
    results = extract_continuation_summaries(session_file)
    assert len(results) == 1
    assert "FastAPI" in results[0]


def test_extract_continuation_summaries_no_summary(tmp_path):
    session_file = tmp_path / "session.jsonl"
    _write_session(
        session_file,
        [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
            },
        ],
    )
    results = extract_continuation_summaries(session_file)
    assert results == []


def test_extract_continuation_summaries_missing_file():
    results = extract_continuation_summaries(Path("/nonexistent/file.jsonl"))
    assert results == []


def test_extract_continuation_summaries_string_content(tmp_path):
    summary = f"{CONTINUATION_MARKER}\n" + "A" * 150
    session_file = tmp_path / "session.jsonl"
    _write_session(
        session_file,
        [
            {"type": "user", "message": {"role": "user", "content": summary}},
        ],
    )
    results = extract_continuation_summaries(session_file)
    assert len(results) == 1


# --- process_session ---


def test_process_session_saves_summary(tmp_path):
    summary_body = "The user is building a FastAPI backend. JWT auth was chosen for stateless scaling. PostgreSQL is the database. This exceeds the minimum length for a valid summary."
    session_file = tmp_path / "-Users-sumithsb-Projects-pluto" / "abc123.jsonl"
    session_file.parent.mkdir(parents=True)
    _write_session(
        session_file,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"{CONTINUATION_MARKER}\n{summary_body}"}],
                },
            },
        ],
    )

    saved = process_session(session_file)
    assert saved == 1

    results = db.search_memories("FastAPI")
    assert len(results) == 1
    assert results[0]["project"] == "pluto"
    assert results[0]["source"] == "hook"


def test_process_session_idempotent(tmp_path):
    summary_body = "The user is building a FastAPI backend. JWT auth was chosen for stateless scaling. PostgreSQL is the database. This exceeds the minimum length for a valid summary."
    session_file = tmp_path / "-Users-sumithsb-Projects-pluto" / "abc123.jsonl"
    session_file.parent.mkdir(parents=True)
    _write_session(
        session_file,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"{CONTINUATION_MARKER}\n{summary_body}"}],
                },
            },
        ],
    )

    first = process_session(session_file)
    second = process_session(session_file)
    assert first == 1
    assert second == 0  # already captured


def test_process_session_no_summary_saves_nothing(tmp_path):
    session_file = tmp_path / "-Users-sumithsb-Projects-pluto" / "xyz.jsonl"
    session_file.parent.mkdir(parents=True)
    _write_session(
        session_file,
        [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
        ],
    )

    saved = process_session(session_file)
    assert saved == 0

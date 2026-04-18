"""
tests/test_capture.py — Tests for brainvault/capture.py
"""

import json
from pathlib import Path

from brainvault import db
from brainvault.adapters.claude_code import (
    CONTINUATION_MARKER,
    ClaudeCodeAdapter,
    chunk_summary,
    clean_continuation_summary,
    extract_continuation_summaries,
    extract_project_name,
)
from brainvault.adapters.cursor import CursorAdapter
from brainvault.capture import _maybe_backfill_embeddings, process_session


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


def test_extract_project_name_short_path_does_not_crash():
    # Directories with fewer than 5 dash-separated segments must not raise
    path = Path("/some/short/dir/session.jsonl")
    result = extract_project_name(path)
    assert isinstance(result, str)
    assert len(result) > 0  # non-empty fallback


def test_extract_project_name_empty_dir_name():
    # Edge case: directory name is just dashes
    path = Path("---/session.jsonl")
    result = extract_project_name(path)
    assert isinstance(result, str)
    assert result  # must not be empty string


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

    saved = process_session(session_file, ClaudeCodeAdapter())
    assert saved == 1

    results = db.search_memories("FastAPI")
    assert len(results) == 1
    assert results[0]["project"] == "pluto"
    assert results[0]["source"] == "hook"
    assert results[0]["source_agent"] == "claude_code"


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

    first = process_session(session_file, ClaudeCodeAdapter())
    second = process_session(session_file, ClaudeCodeAdapter())
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

    saved = process_session(session_file, ClaudeCodeAdapter())
    assert saved == 0


def test_process_session_cursor_user_queries(tmp_path):
    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    session_file = (
        tmp_path
        / "projects"
        / "Users-tester-Projects-myapp"
        / "agent-transcripts"
        / sid
        / f"{sid}.jsonl"
    )
    session_file.parent.mkdir(parents=True)
    long_q = "Plan the database migration and rollback strategy carefully. " * 5
    _write_session(
        session_file,
        [{"role": "user", "message": {"content": [{"type": "text", "text": long_q}]}}],
    )

    saved = process_session(session_file, CursorAdapter())
    assert saved == 1
    rows = db.search_memories("migration")
    assert len(rows) >= 1
    assert rows[0]["source_agent"] == "cursor"
    assert rows[0]["project"] == "myapp"


# ---------------------------------------------------------------------------
# _maybe_run_git_scan
# ---------------------------------------------------------------------------


def test_maybe_run_git_scan_skips_when_latest_commit_already_scanned(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.git_scan as gs
    from brainvault import db

    monkeypatch.chdir(tmp_path)

    repo_key = str(tmp_path)
    commit_hash = "abc123def456abc123def456abc123def456abc1"

    monkeypatch.setattr(gs, "_resolve_repo_path", lambda p: tmp_path)
    monkeypatch.setattr(gs, "_run_git", lambda args, cwd: commit_hash)
    db.mark_commit_scanned(repo_key, commit_hash)

    result = cap._maybe_run_git_scan()
    assert result == 0  # already scanned, skipped


def test_maybe_run_git_scan_runs_when_new_commits_exist(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.git_scan as gs

    monkeypatch.chdir(tmp_path)

    new_hash = "newcommithash000000000000000000000000000"

    monkeypatch.setattr(gs, "_resolve_repo_path", lambda p: tmp_path)
    monkeypatch.setattr(gs, "_run_git", lambda args, cwd: new_hash)

    scan_called = {}

    def fake_scan_repo(repo_path, project, since, limit, verbose):
        scan_called["ran"] = True
        return {
            "commits_saved": 3,
            "commits_examined": 5,
            "already_scanned": 2,
            "not_significant": 0,
        }

    monkeypatch.setattr(gs, "scan_repo", fake_scan_repo)

    result = cap._maybe_run_git_scan()
    assert scan_called.get("ran") is True
    assert result == 3


def test_maybe_run_git_scan_swallows_errors(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.git_scan as gs

    monkeypatch.chdir(tmp_path)

    def boom(p):
        raise ValueError("not a git repo")

    monkeypatch.setattr(gs, "_resolve_repo_path", boom)

    result = cap._maybe_run_git_scan()
    assert result == 0  # error swallowed


# ---------------------------------------------------------------------------
# _maybe_reindex_repo
# ---------------------------------------------------------------------------


def test_maybe_reindex_repo_auto_indexes_first_time(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.code_scan as cs
    import brainvault.git_scan as gs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gs, "_resolve_repo_path", lambda p: tmp_path)

    # Repo has never been indexed; file count is small
    monkeypatch.setattr(cs, "scan_file_tree", lambda p: ([{"f": i} for i in range(10)], 0))

    index_called = {}

    def fake_index_repo(repo_path, project, verbose):
        index_called["ran"] = True
        return {}

    monkeypatch.setattr(cs, "index_repo", fake_index_repo)

    result = cap._maybe_reindex_repo()
    assert index_called.get("ran") is True
    assert result is True


def test_maybe_reindex_repo_skips_when_never_indexed_and_too_large(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.code_scan as cs
    import brainvault.git_scan as gs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gs, "_resolve_repo_path", lambda p: tmp_path)

    # File count exceeds the auto-index limit
    monkeypatch.setattr(cs, "scan_file_tree", lambda p: ([{"f": i} for i in range(6000)], 0))

    result = cap._maybe_reindex_repo()
    assert result is False  # too large, skip


def test_maybe_reindex_repo_skips_when_fresh(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.git_scan as gs
    from brainvault import db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gs, "_resolve_repo_path", lambda p: tmp_path)

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO code_index_runs (repo_path, project, file_count, cochange_pairs) VALUES (?, ?, ?, ?)",
            (str(tmp_path), "myproject", 10, 5),
        )

    result = cap._maybe_reindex_repo()
    assert result is False  # <24h old, skip


def test_maybe_reindex_repo_runs_when_stale(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.code_scan as cs
    import brainvault.git_scan as gs
    from brainvault import db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gs, "_resolve_repo_path", lambda p: tmp_path)

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO code_index_runs (repo_path, project, indexed_at, file_count, cochange_pairs) "
            "VALUES (?, ?, datetime('now', '-2 days'), ?, ?)",
            (str(tmp_path), "myproject", 10, 5),
        )

    index_called = {}

    def fake_index_repo(repo_path, project, verbose):
        index_called["ran"] = True
        return {"files_found": 10, "cochange_pairs": 5, "languages": {}, "parse_errors": 0}

    monkeypatch.setattr(cs, "index_repo", fake_index_repo)

    result = cap._maybe_reindex_repo()
    assert index_called.get("ran") is True
    assert result is True


def test_maybe_reindex_repo_swallows_errors(monkeypatch, tmp_path):
    import brainvault.capture as cap
    import brainvault.git_scan as gs

    monkeypatch.chdir(tmp_path)

    def boom(p):
        raise RuntimeError("unexpected error")

    monkeypatch.setattr(gs, "_resolve_repo_path", boom)

    result = cap._maybe_reindex_repo()
    assert result is False


# ---------------------------------------------------------------------------
# _maybe_backfill_embeddings
# ---------------------------------------------------------------------------


def test_maybe_backfill_embeddings_runs_when_unembedded_exist():
    from brainvault import db

    # Save a memory — mock_embeddings fixture means it IS embedded already via save_memory
    # To get an unembedded one, delete its vector row directly
    mid = db.save_memory("Postgres decision", "decision")
    with db.get_connection() as conn:
        conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (mid,))

    result = _maybe_backfill_embeddings()
    assert result == 1

    # Verify it's now embedded
    assert db.count_embedded() == 1


def test_maybe_backfill_embeddings_skips_when_all_embedded():
    from brainvault import db

    db.save_memory("Already embedded", "pattern")
    # memory_vectors row was created by save_memory via _try_embed_and_store

    result = _maybe_backfill_embeddings()
    assert result == 0


def test_maybe_backfill_embeddings_caps_at_20(monkeypatch):
    from brainvault import db

    # Create 25 unembedded memories
    ids = [db.save_memory(f"decision {i}", "decision") for i in range(25)]
    with db.get_connection() as conn:
        for mid in ids:
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (mid,))
        conn.connection.commit() if hasattr(conn, "connection") else None

    result = _maybe_backfill_embeddings()
    assert result == 20  # capped at 20 per invocation


def test_maybe_backfill_embeddings_swallows_errors(monkeypatch):
    import brainvault.embeddings as emb

    monkeypatch.setattr(emb, "_is_available", lambda: False)

    result = _maybe_backfill_embeddings()
    assert result == 0


# ---------------------------------------------------------------------------
# run() — session_events pruning on Stop hook path
# ---------------------------------------------------------------------------


def test_run_prunes_old_session_events(monkeypatch):
    """capture.run() must call prune_old_events so README retention promise holds."""
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO session_events (session_id, tool_name, input_summary, timestamp)
            VALUES (?, ?, ?, datetime('now', '-100 days'))
            """,
            ("prune-via-capture", "Bash", "echo old"),
        )

    monkeypatch.setattr("brainvault.capture.ALL_ADAPTERS", ())
    monkeypatch.setattr("brainvault.capture._maybe_run_git_scan", lambda: 0)
    monkeypatch.setattr("brainvault.capture._maybe_reindex_repo", lambda: False)
    monkeypatch.setattr("brainvault.capture._maybe_backfill_embeddings", lambda: 0)

    from brainvault.capture import run

    run()
    assert db.get_session_timeline("prune-via-capture") == []

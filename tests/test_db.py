"""
tests/test_db.py — Unit tests for brainvault/db.py
Uses a temporary database so production data is never touched.
"""

from brainvault import db


def test_save_and_retrieve():
    memory_id = db.save_memory(
        content="Always use FastAPI for Python APIs",
        memory_type="pattern",
        project=None,
        keywords=["fastapi", "python", "api"],
    )
    assert memory_id  # should return a uuid string

    results = db.search_memories("FastAPI")
    assert len(results) == 1
    assert results[0]["content"] == "Always use FastAPI for Python APIs"
    assert results[0]["memory_type"] == "pattern"
    assert results[0]["project"] is None


def test_fts5_search_returns_correct_result():
    db.save_memory("JWT auth for stateless APIs", "decision", project="pluto")
    db.save_memory("PostgreSQL for relational data", "decision", project="pluto")
    db.save_memory("Redis for caching layer", "pattern", project="ivy")

    # Use hybrid=False to test FTS5 behaviour specifically.
    # hybrid=True merges vector results (active in test env via mock_embeddings)
    # which can surface all memories regardless of keyword match.
    results = db.search_memories("JWT", hybrid=False)
    assert len(results) == 1
    assert "JWT" in results[0]["content"]


def test_search_project_priority():
    db.save_memory("Use async endpoints always", "pattern", project=None)
    db.save_memory("Use async endpoints in pluto specifically", "decision", project="pluto")

    results = db.search_memories("async endpoints", project="pluto")
    assert len(results) == 2
    # project-scoped memory should rank first
    assert results[0]["project"] == "pluto"


def test_search_no_results():
    results = db.search_memories("xyznonexistenttopic")
    assert results == []


def test_delete_memory():
    memory_id = db.save_memory("Temporary decision", "note")
    results = db.search_memories("Temporary decision")
    assert len(results) == 1

    deleted = db.delete_memory(memory_id)
    assert deleted is True

    results = db.search_memories("Temporary decision")
    assert results == []


def test_delete_nonexistent_memory():
    deleted = db.delete_memory("nonexistent-id-123")
    assert deleted is False


def test_delete_project_memories():
    db.save_memory("decision A", "decision", project="proj_bulk_test")
    db.save_memory("pattern B", "pattern", project="proj_bulk_test")
    db.save_memory("note C", "note", project="proj_bulk_test")

    count = db.delete_project_memories("proj_bulk_test")
    assert count == 3

    results = db.search_memories("proj_bulk_test")
    assert results == []


def test_delete_project_memories_no_match():
    count = db.delete_project_memories("nonexistent_project_xyz")
    assert count == 0


def test_save_project_and_retrieve():
    db.save_project(
        name="pluto",
        description="ML job orchestration API",
        stack=["FastAPI", "PostgreSQL", "Redis"],
        notes="Uses JWT auth",
    )
    project = db.get_project("pluto")
    assert project is not None
    assert project["name"] == "pluto"
    assert project["description"] == "ML job orchestration API"
    assert "FastAPI" in project["stack"]


def test_save_project_upsert():
    db.save_project("pluto", "Old description", ["FastAPI"])
    db.save_project("pluto", "New description", ["FastAPI", "PostgreSQL"])

    project = db.get_project("pluto")
    assert project["description"] == "New description"
    assert "PostgreSQL" in project["stack"]


def test_get_stats():
    db.save_memory("Profile info", "profile")
    db.save_memory("JWT decision", "decision", project="pluto")
    db.save_memory("Async pattern", "pattern")
    db.save_project("pluto", "Test project", ["FastAPI"])

    stats = db.get_stats()
    assert stats["total_memories"] == 3
    assert stats["total_projects"] == 1
    assert stats["by_type"].get("profile") == 1
    assert stats["by_type"].get("decision") == 1
    assert stats["by_type"].get("pattern") == 1
    assert stats["by_project"].get("pluto") == 1
    assert stats["by_project"].get("global") == 2


def test_fts5_special_characters_no_crash():
    db.save_memory("Auth with Bearer tokens", "decision")
    # These should not raise — special chars handled gracefully
    results = db.search_memories('"auth"')
    assert isinstance(results, list)
    results = db.search_memories("auth AND tokens")
    assert isinstance(results, list)


def test_access_count_increments():
    db.save_memory("FastAPI preference", "pattern", keywords=["fastapi"])
    results = db.search_memories("FastAPI")
    assert results[0]["access_count"] == 1

    db.search_memories("FastAPI")
    results = db.search_memories("FastAPI")
    assert results[0]["access_count"] == 3


def test_session_capture_tracking():
    path = "/some/session/file.jsonl"
    assert db.is_session_captured(path) is False

    db.mark_session_captured(path, memory_count=3)
    assert db.is_session_captured(path) is True


def test_list_projects():
    db.save_project("pluto", "API project", ["FastAPI"])
    db.save_project("ivy", "Frontend project", ["React"])

    projects = db.list_projects()
    assert len(projects) == 2
    names = [p["name"] for p in projects]
    assert "pluto" in names
    assert "ivy" in names


def test_get_project_memories():
    db.save_memory("JWT for pluto", "decision", project="pluto")
    db.save_memory("Redis for pluto", "decision", project="pluto")
    db.save_memory("Global pattern", "pattern", project=None)

    memories = db.get_project_memories("pluto")
    assert len(memories) == 2
    assert all(m["project"] == "pluto" for m in memories)


# ---------------------------------------------------------------------------
# VALID_MEMORY_TYPES constant
# ---------------------------------------------------------------------------


def test_valid_memory_types_contains_expected_values():
    assert "profile" in db.VALID_MEMORY_TYPES
    assert "decision" in db.VALID_MEMORY_TYPES
    assert "pattern" in db.VALID_MEMORY_TYPES
    assert "note" in db.VALID_MEMORY_TYPES
    assert "project" in db.VALID_MEMORY_TYPES


def test_valid_memory_types_matches_db_constraint():
    # Every type in VALID_MEMORY_TYPES must be accepted by the DB CHECK constraint
    for t in db.VALID_MEMORY_TYPES:
        mid = db.save_memory("test", t)
        assert mid  # no IntegrityError raised


def test_invalid_memory_type_raises():
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        db.save_memory("bad type", "bogus_type")


# ---------------------------------------------------------------------------
# FTS5 fallback to LIKE search
# ---------------------------------------------------------------------------


def test_fts5_fallback_to_like_on_operational_error(monkeypatch):
    """When FTS5 raises OperationalError, _search_fts must fall back to LIKE."""
    import sqlite3

    db.save_memory("JWT auth for stateless API", "decision", project="proj")

    class _FakeConn:
        """Wraps a real connection but raises OperationalError on MATCH queries."""

        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, params=()):
            if "MATCH" in sql:
                raise sqlite3.OperationalError("fts5: simulated failure")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    from contextlib import contextmanager

    import brainvault.db as db_module

    original_get_connection = db_module.get_connection

    @contextmanager
    def patched_get_connection():
        with original_get_connection() as real_conn:
            yield _FakeConn(real_conn)

    monkeypatch.setattr(db_module, "get_connection", patched_get_connection)

    results = db_module._search_fts("JWT", project=None, limit=5)
    assert len(results) >= 1
    assert any("JWT" in r["content"] for r in results)


def test_hook_capture_duplicate_uses_content_fingerprint():
    db.save_memory(
        "duplicate hook capture text",
        "note",
        project="p1",
        source="hook",
        source_agent="claude_code",
    )
    assert db.is_hook_capture_duplicate(
        "duplicate hook capture text", "p1", source="hook", source_agent="claude_code"
    )
    assert not db.is_hook_capture_duplicate(
        "unique other text", "p1", source="hook", source_agent="claude_code"
    )


def test_audit_vault_counts_cursor_blobs():
    db.save_memory(
        "User queries in Cursor session abc:\n- hello world query text here",
        "note",
        project="p1",
        source="hook",
        source_agent="cursor",
    )
    db.save_memory("Real decision", "decision", project="p1", source="agent")
    audit = db.audit_vault()
    assert audit["cursor_query_blobs"] >= 1
    assert audit["total_memories"] >= 2


def test_prune_memories_dry_run_skips_protected():
    db.save_memory(
        "User queries in Cursor session x:\n- some user query text long enough",
        "note",
        project="p1",
        source="hook",
        source_agent="cursor",
    )
    agent_id = db.save_memory("Keep this agent decision", "decision", source="agent")
    result = db.prune_memories(dry_run=True, cursor_query_blobs=True)
    assert result["candidate_count"] >= 1
    assert agent_id not in result["candidate_ids"]


def test_prune_memories_deletes_cursor_blobs():
    db.save_memory(
        "User queries in Cursor session y:\n- delete me please long query",
        "note",
        project="p1",
        source="hook",
        source_agent="cursor",
    )
    result = db.prune_memories(dry_run=False, cursor_query_blobs=True)
    assert result["deleted"] >= 1
    audit = db.audit_vault()
    assert audit["cursor_query_blobs"] == 0


def test_search_boosts_agent_over_hook_note():
    db.save_memory(
        "User queries in Cursor session x:\n- async endpoints discussion here",
        "note",
        project="pluto",
        source="hook",
        source_agent="cursor",
    )
    db.save_memory(
        "Use async endpoints for all FastAPI routes in pluto",
        "pattern",
        project="pluto",
        source="agent",
    )
    results = db.search_memories("async endpoints pluto", project="pluto", hybrid=False)
    assert len(results) >= 2
    assert results[0]["source"] == "agent"


def test_record_outcome_via_db():
    mid = db.save_memory("Chose Redis for cache", "decision", project="p1")
    assert db.record_outcome(mid, "Worked well in production", sentiment="positive")
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT outcome, outcome_sentiment FROM memories WHERE id = ?", (mid,)
        ).fetchone()
    assert row[0] == "Worked well in production"
    assert row[1] == "positive"


def test_prune_duplicate_hash_keeps_one():
    content = "Exact duplicate content for hash test here"
    db.save_memory(content, "note", project="p1", source="hook", source_agent="cursor")
    db.save_memory(content, "note", project="p1", source="hook", source_agent="cursor")
    result = db.prune_memories(dry_run=False, duplicate_hash=True, duplicate_keep="newest")
    assert result["deleted"] >= 1
    rows = db.search_memories("Exact duplicate content", hybrid=False)
    assert len(rows) == 1


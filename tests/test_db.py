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

    results = db.search_memories("JWT")
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

"""
tests/test_mcp.py — Tests for MCP tool functions in brainvault/mcp_server.py
Tests tool functions directly as Python callables (not via MCP protocol).
"""

from brainvault import db
from brainvault.mcp_server import (
    forget,
    get_my_context,
    get_project,
    get_session_timeline,
    register_project,
    save_memory,
    search_memory,
    update_memory,
)


def test_save_memory_returns_id():
    result = save_memory(
        content="Always use FastAPI for Python backends",
        memory_type="pattern",
    )
    assert "Saved. Memory ID:" in result
    assert "pattern" in result
    assert "global" in result


def test_save_memory_with_project():
    result = save_memory(
        content="JWT for pluto auth",
        memory_type="decision",
        project="pluto",
    )
    assert "Saved. Memory ID:" in result
    assert "project: pluto" in result


def test_save_memory_respects_brainvault_source_agent_env(monkeypatch):
    monkeypatch.setenv("BRAINVAULT_SOURCE_AGENT", "cursor")
    result = save_memory(content="from cursor host", memory_type="note")
    assert "Saved. Memory ID:" in result
    memory_id = result.split("Memory ID: ")[1].split(" ")[0]
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT source_agent FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    assert row["source_agent"] == "cursor"


def test_save_memory_invalid_env_agent_falls_back(monkeypatch):
    monkeypatch.setenv("BRAINVAULT_SOURCE_AGENT", "bogus")
    result = save_memory(content="fallback agent", memory_type="note")
    memory_id = result.split("Memory ID: ")[1].split(" ")[0]
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT source_agent FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    assert row["source_agent"] == "claude_code"


def test_save_memory_invalid_type():
    result = save_memory(content="something", memory_type="invalid_type")
    assert "Invalid memory_type" in result


def test_search_memory_returns_formatted_results():
    save_memory("JWT auth for stateless APIs", "decision", project="pluto")
    result = search_memory("JWT")
    assert "Found 1 memories" in result
    assert "decision" in result
    assert "project: pluto" in result
    assert "JWT auth for stateless APIs" in result


def test_search_memory_no_results():
    result = search_memory("xyznonexistenttopic12345")
    assert "No relevant memory found" in result


def test_search_memory_empty_query():
    result = search_memory("")
    assert "Please provide a search query" in result


def test_search_memory_with_project_filter():
    save_memory("Decision for pluto", "decision", project="pluto")
    save_memory("Decision for ivy", "decision", project="ivy")
    result = search_memory("Decision", project="pluto")
    assert "pluto" in result


def test_get_my_context_empty():
    result = get_my_context()
    assert "No context stored yet" in result
    assert "save_memory" in result


def test_get_my_context_with_data():
    save_memory("I prefer FastAPI over Django", "profile")
    register_project("pluto", "ML job orchestration", ["FastAPI", "PostgreSQL"])
    result = get_my_context()
    assert "FastAPI" in result
    assert "pluto" in result
    assert "ML job orchestration" in result
    assert "## Stats" in result
    assert "memories stored across" in result


def test_register_project():
    result = register_project(
        name="pluto",
        description="ML job orchestration API",
        stack=["FastAPI", "PostgreSQL"],
        notes="Uses JWT auth",
    )
    assert "Project 'pluto' saved" in result
    assert "FastAPI" in result


def test_register_project_empty_name():
    result = register_project(name="", description="test", stack=[])
    assert "cannot be empty" in result


def test_get_project_with_data():
    register_project("pluto", "ML orchestration", ["FastAPI", "PostgreSQL"])
    save_memory("JWT auth decision", "decision", project="pluto")

    result = get_project("pluto")
    assert "Project: pluto" in result
    assert "ML orchestration" in result
    assert "FastAPI" in result
    assert "JWT auth decision" in result


def test_get_project_not_found():
    result = get_project("nonexistent")
    assert "not found" in result
    assert "register_project" in result


def test_get_project_truncates_memories_list():
    register_project("bigproj", "many notes", ["Python"])
    for i in range(25):
        save_memory(f"note-{i}", "note", project="bigproj")

    result = get_project("bigproj")
    assert "## Memories (25)" in result
    assert result.count("- [note]") == 20
    assert "… 5 more" in result
    assert "search_memory" in result


def test_get_session_timeline_truncates_to_recent_events():
    session_id = "trunc-timeline-mcp-session"
    for i in range(60):
        db.record_tool_event(session_id, "Write", f"/src/file{i}.py")

    result = get_session_timeline(session_id)
    assert "60 events recorded" in result
    assert result.count("**Write**") == 50
    assert "10 older event" in result


def test_search_memory_truncates_long_content():
    long_body = "ZUNIQUE_PREFIX_" + ("x" * 500)
    save_memory(long_body, "note")
    result = search_memory("ZUNIQUE_PREFIX_")
    assert "Found 1 memories" in result
    assert "… (id:" in result
    assert len(long_body) > 450


def test_forget_memory():
    save_result = save_memory("Temporary note", "note")
    memory_id = save_result.split("Memory ID: ")[1].split(" ")[0]

    result = forget(memory_id)
    assert "deleted" in result

    search_result = search_memory("Temporary note")
    assert "No relevant memory found" in search_result


def test_forget_nonexistent_memory():
    result = forget("nonexistent-id-abc")
    assert "not found" in result


def test_forget_project():
    save_memory("decision A", "decision", project="mcp_bulk_test")
    save_memory("pattern B", "pattern", project="mcp_bulk_test")

    result = forget(project="mcp_bulk_test")
    assert "2" in result
    assert "mcp_bulk_test" in result

    search_result = search_memory("mcp_bulk_test")
    assert "No relevant memory found" in search_result


def test_forget_project_not_found():
    result = forget(project="nonexistent_project_xyz")
    assert "No memories found" in result


def test_forget_both_args_error():
    result = forget(memory_id="some-id", project="some-project")
    assert "Error" in result


def test_forget_no_args_error():
    result = forget()
    assert "Error" in result


def test_terse_save_and_search_memory(monkeypatch):
    monkeypatch.setenv("BRAINVAULT_MCP_TERSE", "1")
    save_r = save_memory("Terse JWT note", "note", project="tp")
    assert save_r.startswith("ok ")
    assert "note" in save_r
    assert "p=tp" in save_r

    search_r = search_memory("Terse JWT")
    assert search_r.startswith("n=")
    assert "|note|" in search_r
    assert "Terse JWT" in search_r


def test_terse_update_memory_noop(monkeypatch):
    monkeypatch.setenv("BRAINVAULT_MCP_TERSE", "1")
    save_r = save_memory("x", "note")
    mid = save_r.split()[1]
    noop = update_memory(memory_id=mid)
    assert noop == "upd noop"


def test_terse_empty_timeline(monkeypatch):
    monkeypatch.setenv("BRAINVAULT_MCP_TERSE", "1")
    r = get_session_timeline("no-such-session-xyz")
    assert r.startswith("tl - ")

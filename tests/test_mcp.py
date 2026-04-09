"""
tests/test_mcp.py — Tests for MCP tool functions in brainvault/mcp_server.py
Tests tool functions directly as Python callables (not via MCP protocol).
"""

from brainvault.mcp_server import (
    forget,
    get_my_context,
    get_project,
    register_project,
    save_memory,
    search_memory,
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

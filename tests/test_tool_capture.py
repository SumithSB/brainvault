"""
tests/test_tool_capture.py — Unit tests for brainvault/tool_capture.py
All subprocess / stdin I/O is mocked. DB autouse fixture handles isolation.
"""

import json

from brainvault import db
from brainvault.tool_capture import (
    CAPTURED_TOOLS,
    _derive_session_id,
    _infer_project,
    _redact_sensitive,
    _summarize_input,
    _summarize_output,
    process_event,
)

# ---------------------------------------------------------------------------
# _summarize_input
# ---------------------------------------------------------------------------


def test_summarize_input_write():
    result = _summarize_input("Write", {"file_path": "/some/file.py"})
    assert "file.py" in result


def test_summarize_input_notebook_edit():
    result = _summarize_input("NotebookEdit", {"file_path": "/nb.ipynb"})
    assert "nb.ipynb" in result


def test_summarize_input_edit_with_old_string():
    result = _summarize_input("Edit", {"file_path": "/a.py", "old_string": "def old_func():"})
    assert "a.py" in result
    assert "old_func" in result


def test_summarize_input_edit_no_old_string():
    result = _summarize_input("Edit", {"file_path": "/b.py"})
    assert "b.py" in result


def test_summarize_input_bash():
    result = _summarize_input("Bash", {"command": "pytest tests/ -v"})
    assert "pytest" in result


def test_summarize_input_bash_truncates_long_command():
    long_cmd = "echo " + "x" * 400
    result = _summarize_input("Bash", {"command": long_cmd})
    assert len(result) <= 300


def test_summarize_input_bash_redacts_bearer():
    result = _summarize_input(
        "Bash", {"command": "curl -H 'Authorization: Bearer sk-secret-token-123' https://x"}
    )
    assert "sk-secret-token-123" not in result
    assert "<redacted>" in result


def test_redact_sensitive_sk_prefix():
    assert "sk-abc" not in _redact_sensitive("token sk-abcdefghijklmnopqrstuvwxyz here")


def test_summarize_input_todowrite():
    todos = [
        {"content": "task1", "status": "pending"},
        {"content": "task2", "status": "completed"},
        {"content": "task3", "status": "pending"},
    ]
    result = _summarize_input("TodoWrite", {"todos": todos})
    assert "3 todos" in result
    assert "2 pending" in result


def test_summarize_input_todowrite_empty():
    result = _summarize_input("TodoWrite", {"todos": []})
    assert "0 todos" in result


# ---------------------------------------------------------------------------
# _summarize_output
# ---------------------------------------------------------------------------


def test_summarize_output_bash_success():
    result = _summarize_output("Bash", {"exit_code": 0})
    assert "exit=0" in result


def test_summarize_output_bash_failure():
    result = _summarize_output("Bash", {"exit_code": 1, "stderr": "command not found"})
    assert "exit=1" in result
    assert "command not found" in result


def test_summarize_output_non_bash():
    result = _summarize_output("Write", {"output": "ok"})
    assert result == ""


# ---------------------------------------------------------------------------
# _derive_session_id
# ---------------------------------------------------------------------------


def test_derive_session_id_from_transcript_path():
    payload = {"transcript_path": "/home/user/.claude/projects/myproject/abc-123-uuid.jsonl"}
    result = _derive_session_id(payload)
    assert result == "abc-123-uuid"


def test_derive_session_id_missing_transcript_path():
    result = _derive_session_id({})
    # Falls back to date string YYYY-MM-DD
    import re

    assert re.match(r"\d{4}-\d{2}-\d{2}", result)


# ---------------------------------------------------------------------------
# _infer_project
# ---------------------------------------------------------------------------


def test_infer_project_from_transcript_path():
    # ~/.claude/projects/-Users-foo-Projects-myproject/<uuid>.jsonl
    payload = {
        "transcript_path": "/Users/foo/.claude/projects/-Users-foo-Projects-myproject/abc.jsonl"
    }
    result = _infer_project(payload)
    assert result == "myproject"


def test_infer_project_missing_path():
    result = _infer_project({})
    assert result is None


def test_infer_project_non_encoded_parent():
    # If parent doesn't start with '-', return None
    payload = {"transcript_path": "/some/other/path/session.jsonl"}
    result = _infer_project(payload)
    assert result is None


# ---------------------------------------------------------------------------
# CAPTURED_TOOLS
# ---------------------------------------------------------------------------


def test_captured_tools_contains_expected():
    assert "Write" in CAPTURED_TOOLS
    assert "Edit" in CAPTURED_TOOLS
    assert "Bash" in CAPTURED_TOOLS
    assert "TodoWrite" in CAPTURED_TOOLS
    assert "NotebookEdit" in CAPTURED_TOOLS


def test_captured_tools_excludes_read():
    assert "Read" not in CAPTURED_TOOLS
    assert "Grep" not in CAPTURED_TOOLS
    assert "Glob" not in CAPTURED_TOOLS


# ---------------------------------------------------------------------------
# process_event — DB integration
# ---------------------------------------------------------------------------


def _make_payload(tool_name, tool_input, tool_response=None, session_id="test-session-001"):
    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response or {},
        "transcript_path": f"/foo/.claude/projects/-foo-bar/{session_id}.jsonl",
    }


def test_process_event_write_saves_to_db():
    payload = _make_payload("Write", {"file_path": "/src/main.py"})
    process_event(payload)

    events = db.get_session_timeline("test-session-001")
    assert len(events) == 1
    assert events[0]["tool_name"] == "Write"
    assert "main.py" in events[0]["input_summary"]


def test_process_event_bash_saves_to_db():
    payload = _make_payload("Bash", {"command": "pytest tests/"})
    process_event(payload)

    events = db.get_session_timeline("test-session-001")
    assert len(events) == 1
    assert events[0]["tool_name"] == "Bash"
    assert "pytest" in events[0]["input_summary"]


def test_process_event_ignored_tool_not_saved():
    payload = _make_payload("Read", {"file_path": "/some/file.py"})
    process_event(payload)

    # Should not be saved since Read is not in CAPTURED_TOOLS
    events = db.get_session_timeline("test-session-001")
    assert len(events) == 0


def test_process_event_multiple_tools_same_session():
    session = "multi-tool-session"
    for tool, inp in [
        ("Write", {"file_path": "/a.py"}),
        ("Edit", {"file_path": "/b.py", "old_string": "old"}),
        ("Bash", {"command": "git status"}),
    ]:
        process_event(_make_payload(tool, inp, session_id=session))

    events = db.get_session_timeline(session)
    assert len(events) == 3
    assert [e["tool_name"] for e in events] == ["Write", "Edit", "Bash"]


def test_process_event_bash_exit_code_in_output():
    payload = _make_payload(
        "Bash",
        {"command": "bad-cmd"},
        tool_response={"exit_code": 127, "stderr": "not found"},
    )
    process_event(payload)

    events = db.get_session_timeline("test-session-001")
    assert "127" in events[0]["output_summary"]


# ---------------------------------------------------------------------------
# DB functions: get_recent_activity and prune_old_events
# ---------------------------------------------------------------------------


def test_get_recent_activity_returns_sessions():
    session = "activity-test-session"
    process_event(_make_payload("Write", {"file_path": "/x.py"}, session_id=session))
    process_event(_make_payload("Bash", {"command": "ls"}, session_id=session))

    data = db.get_recent_activity(days=7)
    assert data["total_events"] >= 2
    session_ids = [s["session_id"] for s in data["sessions"]]
    assert session in session_ids


def test_get_recent_activity_tools_list():
    session = "tools-list-session"
    process_event(_make_payload("Write", {"file_path": "/f.py"}, session_id=session))
    process_event(_make_payload("Bash", {"command": "pytest"}, session_id=session))

    data = db.get_recent_activity(days=7)
    matching = next(s for s in data["sessions"] if s["session_id"] == session)
    assert "Write" in matching["tools"]
    assert "Bash" in matching["tools"]


def test_get_recent_activity_empty():
    data = db.get_recent_activity(days=7)
    assert data["total_events"] == 0
    assert data["sessions"] == []


def test_prune_old_events_removes_nothing_when_all_recent():
    process_event(_make_payload("Write", {"file_path": "/new.py"}))
    deleted = db.prune_old_events(days=90)
    assert deleted == 0


def test_prune_old_events_removes_old_rows(tmp_db):
    """Directly insert an old row and verify prune removes it."""
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO session_events (session_id, tool_name, input_summary, timestamp)
            VALUES (?, ?, ?, datetime('now', '-100 days'))
            """,
            ("old-session", "Bash", "echo hi"),
        )

    deleted = db.prune_old_events(days=90)
    assert deleted == 1

    events = db.get_session_timeline("old-session")
    assert events == []


# ---------------------------------------------------------------------------
# run() — smoke test: malformed stdin does not raise
# ---------------------------------------------------------------------------


def test_run_swallows_malformed_payload(monkeypatch):
    import io

    import brainvault.tool_capture as tc

    monkeypatch.setattr("sys.stdin", io.StringIO("not-json{{"))
    # Should complete without raising
    tc.run()


def test_run_processes_valid_payload(monkeypatch):
    import io

    import brainvault.tool_capture as tc

    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/run-test.py"},
            "tool_response": {},
            "transcript_path": "/foo/.claude/projects/-bar/run-test-session.jsonl",
        }
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    tc.run()

    events = db.get_session_timeline("run-test-session")
    assert len(events) == 1
    assert "run-test.py" in events[0]["input_summary"]

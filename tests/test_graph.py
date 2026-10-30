"""Tests for brainvault graph HTML generation (XSS hardening, render sanity)."""

from __future__ import annotations

from brainvault.git_scan import _format_memory_content
from brainvault.graph import _parse_git_memory, render_html


def _minimal_graph_data(**overrides) -> dict:
    base = {
        "nodes": [],
        "edges": [],
        "stats": {"total_nodes": 0, "total_edges": 0, "by_type": {}, "by_source": {}},
    }
    base.update(overrides)
    return base


def test_parse_git_memory_ignores_header_lookalikes_in_message_body():
    """Commit bodies are stored after a blank line and must not overwrite header fields.

    File overlap and temporal edges in the graph are built from those fields.
    """
    content = _format_memory_content(
        {
            "hash": "abc12345deadbeef",
            "short_hash": "abc12345",
            "message": "fix parser",
            "author": "Ada <ada@example.com>",
            "date": "2024-01-15T10:00:00+00:00",
            "is_merge": False,
        },
        {
            "files_changed": 2,
            "additions": 10,
            "deletions": 3,
            "top_files": ["brainvault/graph.py", "tests/test_graph.py"],
        },
        body=(
            "The diff discussion mentions a different path.\n"
            "Files: not-a-real-file.py\n"
            "Date: 1999-01-01\n"
            "Author: Someone Else <else@example.com>\n"
            "Changed: 99 files, +1 -1 lines"
        ),
    )
    parsed = _parse_git_memory(content)
    assert parsed["is_git"] is True
    assert parsed["commit_hash"] == "abc12345"
    assert parsed["subject"] == "fix parser"
    assert parsed["date"] == "2024-01-15"
    assert parsed["author"] == "Ada <ada@example.com>"
    assert parsed["files_changed"] == 2
    assert parsed["additions"] == 10
    assert parsed["deletions"] == 3
    assert parsed["files"] == ["brainvault/graph.py", "tests/test_graph.py"]


def test_parse_git_memory_non_git_content_is_not_a_commit():
    parsed = _parse_git_memory("Just a note about FastAPI")
    assert parsed["is_git"] is False
    assert parsed["commit_hash"] is None
    assert parsed["files"] == []


def test_render_html_injects_data_placeholder():
    html = render_html(_minimal_graph_data())
    assert "__GRAPH_DATA__" not in html
    assert "const DATA = " in html


def test_render_html_no_inline_selectnode_onclick():
    """Connected nodes must not use inline onclick with raw node ids (XSS)."""
    html = render_html(_minimal_graph_data())
    assert 'onclick="selectNode' not in html
    assert "bv-connected-mount" in html


def test_render_html_escapes_json_for_script_context():
    """Memory text containing </script> must not break out of the JSON blob."""
    malicious = "hello</script><script>alert(1)</script>"
    data = _minimal_graph_data(
        nodes=[
            {
                "id": "n1",
                "label": malicious,
                "full_content": malicious,
                "type": "note",
                "source": "explicit",
                "project": None,
                "keywords": [],
                "access_count": 0,
                "created_at": "2024-01-01",
                "outcome": None,
                "outcome_sentiment": None,
                "size": 8,
            }
        ],
        stats={
            "total_nodes": 1,
            "total_edges": 0,
            "by_type": {"note": 1},
            "by_source": {"explicit": 1},
        },
    )
    html = render_html(data)
    assert (
        "<\\/script>" in html
        or "</script>" not in html.split("const DATA = ", 1)[1].split("</script>", 1)[0]
    )

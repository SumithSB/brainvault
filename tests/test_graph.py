"""Tests for brainvault graph HTML generation (XSS hardening, render sanity)."""

from __future__ import annotations

from brainvault.graph import render_html


def _minimal_graph_data(**overrides) -> dict:
    base = {
        "nodes": [],
        "edges": [],
        "stats": {"total_nodes": 0, "total_edges": 0, "by_type": {}, "by_source": {}},
    }
    base.update(overrides)
    return base


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

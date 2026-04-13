"""
brainvault/graph.py — Generate a self-contained HTML brain graph of all memories.

Called via CLI:
    brainvault graph [--output <path>] [--open]

Produces a single HTML file with a D3.js force-directed graph.
No server required — open the file directly in any browser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from brainvault import db

# ---------------------------------------------------------------------------
# Git memory parser
# ---------------------------------------------------------------------------


def _parse_git_memory(content: str) -> dict:
    """
    Extract structured fields from a [git] memory string.

    Expected format:
        [git] abc12345: subject line
        Date: 2024-01-15
        Author: Name <email>
        Changed: 5 files, +120 -45 lines
        Files: a.py, b.py, c.py
    """
    result: dict = {
        "is_git": False,
        "commit_hash": None,
        "subject": None,
        "date": None,
        "author": None,
        "files_changed": 0,
        "additions": 0,
        "deletions": 0,
        "files": [],
    }
    lines = content.strip().split("\n")
    if not lines[0].startswith("[git]"):
        return result

    result["is_git"] = True
    m = re.match(r"\[git\]\s+([a-f0-9]+):\s+(.+)", lines[0])
    if m:
        result["commit_hash"] = m.group(1)
        result["subject"] = m.group(2)

    for line in lines[1:]:
        if line.startswith("Date: "):
            result["date"] = line[6:].strip()
        elif line.startswith("Author: "):
            result["author"] = line[8:].strip()
        elif line.startswith("Changed: "):
            cm = re.match(r"Changed: (\d+) files, \+(\d+) -(\d+)", line)
            if cm:
                result["files_changed"] = int(cm.group(1))
                result["additions"] = int(cm.group(2))
                result["deletions"] = int(cm.group(3))
        elif line.startswith("Files: "):
            result["files"] = [f.strip() for f in line[7:].split(",") if f.strip()]

    return result


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------


def build_graph_data() -> dict:
    """
    Query the DB and return a JSON-serialisable dict with nodes and edges.

    Node types:
      - "project"  — one per project row
      - "decision" / "pattern" / "note" / "profile" — one per memory
      - "file"     — one per indexed source file (from code_entities)

    Each memory node also carries:
      - source: "agent" | "git" | "bootstrap" | "explicit"
      - git metadata if source=="git"

    Edges:
      - belongs_to       — memory/file → project
      - keyword_overlap  — memories sharing ≥2 keywords
      - file_overlap     — git commits sharing ≥1 changed file
      - temporal         — consecutive git commits in the same project
      - cochange         — files that change together in git history
      - memory_file      — git commit memory → indexed file it touched
    """
    with db.get_connection() as conn:
        memories = [
            dict(r)
            for r in conn.execute(
                """SELECT id, content, memory_type, project, keywords,
                          source, access_count, created_at, outcome, outcome_sentiment
                   FROM memories"""
            ).fetchall()
        ]
        projects = [
            dict(r)
            for r in conn.execute(
                "SELECT name, description, stack, status FROM projects"
            ).fetchall()
        ]
        links_rows = conn.execute(
            "SELECT from_id, to_id, relationship, weight FROM memory_links"
        ).fetchall()

        # Code intelligence tables (populated by `brainvault index-repo`)
        tables_present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        code_file_rows: list[tuple] = []
        code_cochange_rows: list[tuple] = []
        if (
            "code_entities" in tables_present
            and "code_cochange" in tables_present
            and "code_index_runs" in tables_present
        ):
            index_runs = conn.execute("SELECT repo_path, project FROM code_index_runs").fetchall()
            for repo_path, project in index_runs:
                top_files = conn.execute(
                    """
                    SELECT ce.file_path, ce.language,
                           COALESCE(SUM(cc.cochange_count), 0) AS total
                    FROM code_entities ce
                    LEFT JOIN (
                        SELECT file_a AS file_path, cochange_count
                        FROM code_cochange WHERE repo_path=?
                        UNION ALL
                        SELECT file_b AS file_path, cochange_count
                        FROM code_cochange WHERE repo_path=?
                    ) cc ON ce.file_path = cc.file_path
                    WHERE ce.repo_path=?
                    GROUP BY ce.file_path
                    ORDER BY total DESC LIMIT 40
                    """,
                    (repo_path, repo_path, repo_path),
                ).fetchall()
                for fp, lang, total in top_files:
                    code_file_rows.append((repo_path, project, fp, lang, total))

                top_pairs = conn.execute(
                    """
                    SELECT file_a, file_b, cochange_count
                    FROM code_cochange
                    WHERE repo_path=?
                    ORDER BY cochange_count DESC LIMIT 60
                    """,
                    (repo_path,),
                ).fetchall()
                for fa, fb, cnt in top_pairs:
                    code_cochange_rows.append((repo_path, fa, fb, cnt))

    nodes: list[dict] = []
    edges: list[dict] = []

    # Project nodes
    for p in projects:
        nid = f"proj:{p['name']}"
        stack = json.loads(p["stack"]) if isinstance(p["stack"], str) else p["stack"]
        nodes.append(
            {
                "id": nid,
                "label": p["name"],
                "type": "project",
                "source": "project",
                "description": p["description"],
                "stack": stack,
                "status": p["status"],
                "size": 22,
            }
        )

    # Memory nodes
    keyword_map: dict[str, list[str]] = {}
    git_file_map: dict[str, list[str]] = {}  # memory_id → files (git only)
    git_by_project: dict[str, list[dict]] = {}  # project → sorted git memories

    for m in memories:
        try:
            kws = (
                json.loads(m["keywords"])
                if isinstance(m["keywords"], str)
                else (m["keywords"] or [])
            )
        except (json.JSONDecodeError, TypeError):
            kws = []
        keyword_map[m["id"]] = kws

        git_meta = _parse_git_memory(m["content"])

        # Determine label
        if git_meta["is_git"] and git_meta["subject"]:
            label = git_meta["subject"][:100]
        else:
            label = m["content"][:100]

        node: dict = {
            "id": m["id"],
            "label": label,
            "full_content": m["content"],
            "type": m["memory_type"],
            "source": m.get("source") or "explicit",
            "project": m["project"],
            "keywords": kws,
            "access_count": m["access_count"] or 0,
            "created_at": (m["created_at"] or "")[:10],
            "outcome": m["outcome"],
            "outcome_sentiment": m["outcome_sentiment"],
            "size": 8 + min((m["access_count"] or 0) * 2, 16),
        }

        if git_meta["is_git"]:
            node.update(
                {
                    "git_hash": git_meta["commit_hash"],
                    "git_date": git_meta["date"],
                    "git_author": git_meta["author"],
                    "git_files_changed": git_meta["files_changed"],
                    "git_additions": git_meta["additions"],
                    "git_deletions": git_meta["deletions"],
                    "git_files": git_meta["files"],
                }
            )
            git_file_map[m["id"]] = git_meta["files"]
            proj_key = m["project"] or "__global__"
            git_by_project.setdefault(proj_key, []).append(
                {
                    "id": m["id"],
                    "date": git_meta["date"] or "",
                }
            )

        nodes.append(node)

        # belongs_to edge
        if m["project"]:
            edges.append(
                {
                    "source": m["id"],
                    "target": f"proj:{m['project']}",
                    "type": "belongs_to",
                    "weight": 2.0,
                }
            )

    # Keyword overlap edges (≥2 shared keywords)
    ids = [m["id"] for m in memories]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            shared = set(keyword_map[a]) & set(keyword_map[b])
            if len(shared) >= 2:
                edges.append(
                    {
                        "source": a,
                        "target": b,
                        "type": "keyword_overlap",
                        "weight": min(len(shared) / 3.0, 1.0),
                        "shared_keywords": list(shared)[:5],
                    }
                )

    # File overlap edges between git commits (same file touched)
    git_ids = list(git_file_map.keys())
    for i in range(len(git_ids)):
        for j in range(i + 1, len(git_ids)):
            a, b = git_ids[i], git_ids[j]
            shared_files = set(git_file_map[a]) & set(git_file_map[b])
            if shared_files:
                edges.append(
                    {
                        "source": a,
                        "target": b,
                        "type": "file_overlap",
                        "weight": min(len(shared_files) / 3.0, 1.0),
                        "shared_files": list(shared_files)[:4],
                    }
                )

    # Temporal edges: consecutive git commits in same project (sorted by date)
    for proj_key, git_list in git_by_project.items():
        sorted_commits = sorted(git_list, key=lambda x: x["date"])
        for i in range(len(sorted_commits) - 1):
            edges.append(
                {
                    "source": sorted_commits[i]["id"],
                    "target": sorted_commits[i + 1]["id"],
                    "type": "temporal",
                    "weight": 0.5,
                }
            )

    # Code file nodes
    file_nodes_added: set[str] = set()
    for repo_path, project, file_path, language, cochange_score in code_file_rows:
        nid = f"file:{repo_path}:{file_path}"
        file_nodes_added.add(nid)
        nodes.append(
            {
                "id": nid,
                "label": Path(file_path).name,
                "full_path": file_path,
                "type": "file",
                "source": "code",
                "project": project,
                "language": language or "unknown",
                "cochange_score": int(cochange_score or 0),
                "size": 6 + min(int(cochange_score or 0) // 5, 12),
            }
        )
        if project:
            edges.append(
                {
                    "source": nid,
                    "target": f"proj:{project}",
                    "type": "belongs_to",
                    "weight": 1.0,
                }
            )

    # Co-change edges between file nodes
    for repo_path, file_a, file_b, count in code_cochange_rows:
        id_a = f"file:{repo_path}:{file_a}"
        id_b = f"file:{repo_path}:{file_b}"
        if id_a in file_nodes_added and id_b in file_nodes_added:
            edges.append(
                {
                    "source": id_a,
                    "target": id_b,
                    "type": "cochange",
                    "weight": min(count / 10.0, 1.0),
                    "cochange_count": count,
                }
            )

    # memory_file edges: git commit memories → file nodes they touched
    file_name_map: dict[str, list[str]] = {}
    for nid in file_nodes_added:
        fname = Path(nid.split(":", 2)[2]).name
        file_name_map.setdefault(fname, []).append(nid)

    for mem_id, files in git_file_map.items():
        for f in files:
            fname = Path(f).name
            for fnode_id in file_name_map.get(fname, []):
                edges.append(
                    {
                        "source": mem_id,
                        "target": fnode_id,
                        "type": "memory_file",
                        "weight": 0.3,
                    }
                )

    # memory_links table edges
    for row in links_rows:
        edges.append(
            {
                "source": row[0],
                "target": row[1],
                "type": row[2],
                "weight": row[3],
            }
        )

    # Stats
    type_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for n in nodes:
        type_counts[n["type"]] = type_counts.get(n["type"], 0) + 1
        source_counts[n["source"]] = source_counts.get(n["source"], 0) + 1

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "by_type": type_counts,
            "by_source": source_counts,
        },
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Brainvault — Memory Graph</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 120'%3E%3Ccircle cx='60' cy='60' r='58' fill='%230d1117' stroke='%2330363d' stroke-width='1.5'/%3E%3Cpath d='M60 38 C60 38 50 36 44 40 C38 44 36 50 37 56 C36 58 34 61 35 65 C36 69 40 71 43 70 C44 73 46 76 50 77 C54 78 58 76 60 74' fill='none' stroke='%2358a6ff' stroke-width='2.2' stroke-linecap='round'/%3E%3Cpath d='M60 38 C60 38 70 36 76 40 C82 44 84 50 83 56 C84 58 86 61 85 65 C84 69 80 71 77 70 C76 73 74 76 70 77 C66 78 62 76 60 74' fill='none' stroke='%2358a6ff' stroke-width='2.2' stroke-linecap='round'/%3E%3Ccircle cx='49' cy='54' r='3' fill='%2358a6ff'/%3E%3Ccircle cx='71' cy='54' r='3' fill='%2358a6ff'/%3E%3Ccircle cx='60' cy='50' r='2.5' fill='%233fb950'/%3E%3Ccircle cx='55' cy='64' r='2.5' fill='%23a371f7'/%3E%3Ccircle cx='65' cy='64' r='2.5' fill='%23a371f7'/%3E%3Ccircle cx='60' cy='92' r='7' fill='none' stroke='%2358a6ff' stroke-width='1.8'/%3E%3Crect x='57.5' y='92' width='5' height='6' rx='1' fill='%2358a6ff' opacity='.8'/%3E%3Ccircle cx='60' cy='92' r='2.5' fill='%2358a6ff'/%3E%3C/svg%3E"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; overflow: hidden; }
#app { display: flex; height: 100vh; }

/* ── Sidebar ── */
#sidebar { width: 340px; min-width: 340px; background: #161b22; border-right: 1px solid #30363d; display: flex; flex-direction: column; overflow: hidden; }
#sidebar-header { padding: 14px 16px 10px; border-bottom: 1px solid #30363d; }
#sidebar-header h1 { font-size: 14px; font-weight: 600; color: #58a6ff; margin-bottom: 3px; }
#sidebar-header .stats { font-size: 11px; color: #8b949e; }
#search-box { margin: 10px 14px 0; padding: 7px 10px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #e6edf3; font-size: 12px; width: calc(100% - 28px); outline: none; }
#search-box:focus { border-color: #58a6ff; }

/* ── Filter section ── */
#filters { padding: 10px 14px; border-bottom: 1px solid #30363d; }
.filter-row { margin-bottom: 8px; }
.filter-row:last-child { margin-bottom: 0; }
.filter-label { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 5px; }
.filter-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chip { padding: 3px 9px; border-radius: 12px; font-size: 11px; cursor: pointer; border: 1px solid transparent; user-select: none; transition: opacity 0.15s; }
.chip.inactive { opacity: 0.3; }

/* ── Legend ── */
#legend { padding: 8px 14px; border-bottom: 1px solid #30363d; }
.legend-label { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 5px; }
.legend-items { display: flex; flex-wrap: wrap; gap: 8px; }
.legend-item { font-size: 10px; color: #8b949e; display: flex; align-items: center; gap: 4px; }
.legend-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.legend-line { width: 18px; height: 2px; flex-shrink: 0; }

/* ── Detail panel ── */
#detail-panel { flex: 1; overflow-y: auto; padding: 14px; }
#detail-panel .placeholder { color: #8b949e; font-size: 12px; line-height: 1.6; }
#detail-type-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; margin-bottom: 8px; padding: 3px 8px; border-radius: 12px; }
#detail-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; line-height: 1.5; }
#detail-meta { font-size: 11px; color: #8b949e; margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 8px; }
#detail-meta span { display: flex; align-items: center; gap: 3px; }
.git-card { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px; margin-bottom: 10px; font-size: 11px; }
.git-card .git-hash { font-family: monospace; color: #f0883e; margin-bottom: 6px; font-size: 12px; }
.git-card .git-row { color: #8b949e; margin-bottom: 3px; }
.git-card .git-row strong { color: #e6edf3; }
.git-card .git-stat { display: inline-flex; gap: 10px; margin-top: 6px; }
.git-card .git-add { color: #3fb950; }
.git-card .git-del { color: #ff7b72; }
.git-files { margin-top: 6px; }
.git-file-tag { display: inline-block; background: #161b22; border: 1px solid #30363d; border-radius: 3px; padding: 1px 5px; margin: 2px; font-size: 10px; color: #d2a8ff; font-family: monospace; }
#detail-content { font-size: 12px; line-height: 1.6; white-space: pre-wrap; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px; margin-bottom: 10px; word-break: break-word; }
#detail-outcome { font-size: 11px; padding: 8px 10px; border-radius: 6px; margin-bottom: 8px; }
#detail-keywords { font-size: 11px; color: #8b949e; margin-bottom: 10px; }
#detail-keywords span { display: inline-block; background: #21262d; border-radius: 4px; padding: 2px 6px; margin: 2px; }
.connected-node { font-size: 11px; margin-top: 4px; cursor: pointer; padding: 5px 7px; border-radius: 4px; background: #0d1117; border: 1px solid transparent; }
.connected-node:hover { border-color: #30363d; }

/* ── Graph area ── */
#graph { flex: 1; position: relative; }
svg { width: 100%; height: 100%; }

/* Project hulls */
.hull { fill-opacity: 0.06; stroke-opacity: 0.3; stroke-width: 2; rx: 10; }

.link { stroke-opacity: 0.35; }
.link.belongs_to { stroke: #388bfd; stroke-opacity: 0.5; stroke-width: 1.5; }
.link.keyword_overlap { stroke: #3fb950; stroke-opacity: 0.2; stroke-dasharray: 4,4; stroke-width: 1; }
.link.file_overlap { stroke: #f0883e; stroke-opacity: 0.4; stroke-width: 1.5; stroke-dasharray: 2,3; }
.link.temporal { stroke: #d2a8ff; stroke-opacity: 0.25; stroke-dasharray: 6,4; stroke-width: 1; }
.link.cochange { stroke: #20b2aa; stroke-opacity: 0.55; stroke-width: 1.5; }
.link.memory_file { stroke: #20b2aa; stroke-opacity: 0.25; stroke-dasharray: 3,4; stroke-width: 1; }
.link.faded { opacity: 0.03; }

.node circle { stroke-width: 1.5px; cursor: pointer; transition: filter 0.15s; }
.node circle:hover { filter: brightness(1.35); }
.node.git-node circle { stroke-dasharray: none; stroke-width: 2.5px; }
.node.selected circle { stroke-width: 3px; stroke: #fff !important; }
.node polygon { stroke-width: 1.5px; cursor: pointer; transition: filter 0.15s; }
.node polygon:hover { filter: brightness(1.35); }
.node.selected polygon { stroke-width: 3px; stroke: #fff !important; }
.node.faded { opacity: 0.1; }
.node text { font-size: 9px; fill: #8b949e; pointer-events: none; text-anchor: middle; dominant-baseline: central; }
.node.project text { font-size: 11px; font-weight: 600; fill: #e6edf3; }

/* ── Tooltip ── */
#tooltip { position: fixed; background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 8px 12px; font-size: 11px; max-width: 280px; pointer-events: none; z-index: 100; display: none; line-height: 1.5; }
#tooltip .tt-type { color: #8b949e; margin-bottom: 3px; font-size: 10px; }
#tooltip .tt-content { color: #e6edf3; }
#tooltip .tt-git { color: #f0883e; font-family: monospace; font-size: 10px; margin-top: 3px; }
#tooltip .tt-files { color: #8b949e; font-size: 10px; margin-top: 3px; }

/* ── Controls ── */
#controls { position: absolute; bottom: 16px; right: 16px; display: flex; flex-direction: column; gap: 6px; }
.ctrl-btn { width: 32px; height: 32px; background: #161b22; border: 1px solid #30363d; border-radius: 6px; color: #e6edf3; font-size: 16px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
.ctrl-btn:hover { background: #21262d; }

/* ── Edge toggle ── */
#edge-toggles { position: absolute; top: 14px; right: 14px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px 12px; font-size: 11px; }
#edge-toggles .et-title { color: #8b949e; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 7px; }
.et-row { display: flex; align-items: center; gap: 7px; margin-bottom: 5px; cursor: pointer; user-select: none; }
.et-row:last-child { margin-bottom: 0; }
.et-swatch { width: 20px; height: 2px; flex-shrink: 0; }
.et-label { color: #e6edf3; }
.et-row.inactive .et-label { opacity: 0.35; }
</style>
</head>
<body>
<div id="app">
  <div id="sidebar">
    <div id="sidebar-header">
      <h1>Brainvault — Memory Graph</h1>
      <div class="stats" id="header-stats"></div>
    </div>
    <input id="search-box" type="text" placeholder="Search memories, keywords, files…">
    <div id="filters">
      <div class="filter-row">
        <div class="filter-label">Layer</div>
        <div class="filter-chips" id="chips-layer"></div>
      </div>
      <div class="filter-row">
        <div class="filter-label">Memory type</div>
        <div class="filter-chips" id="chips-type"></div>
      </div>
      <div class="filter-row">
        <div class="filter-label">Source</div>
        <div class="filter-chips" id="chips-source"></div>
      </div>
    </div>
    <div id="legend">
      <div class="legend-label">Edge types</div>
      <div class="legend-items" id="legend-edges"></div>
    </div>
    <div id="detail-panel">
      <div class="placeholder">Click a node to inspect it.<br><br>
        <strong style="color:#e6edf3">Node colours</strong><br>
        Orange = projects &nbsp; Blue = decisions<br>
        Green = patterns &nbsp; Purple = notes<br>
        Red = profile &nbsp; <span style="color:#20b2aa">Teal ◆ = code files</span><br><br>
        <strong style="color:#e6edf3">Git nodes</strong> have a bright ring.<br><br>
        <strong style="color:#e6edf3">Edge types</strong><br>
        Solid blue = project membership<br>
        Orange dashed = same files changed<br>
        Purple dashed = temporal (commit order)<br>
        Green dashed = shared keywords<br>
        <span style="color:#20b2aa">Teal solid = co-changed files<br>
        Teal dashed = commit → file</span><br><br>
        Run <code style="color:#f0883e">brainvault index-repo .</code><br>
        to add code file nodes.
      </div>
    </div>
  </div>
  <div id="graph">
    <svg id="svg">
      <defs>
        <marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L0,6 L6,3 z" fill="#8b949e" opacity="0.4"/>
        </marker>
      </defs>
    </svg>
    <div id="tooltip"></div>
    <div id="edge-toggles">
      <div class="et-title">Show edges</div>
    </div>
    <div id="controls">
      <button class="ctrl-btn" id="btn-zoom-in" title="Zoom in">+</button>
      <button class="ctrl-btn" id="btn-zoom-out" title="Zoom out">−</button>
      <button class="ctrl-btn" id="btn-reset" title="Reset view">⌂</button>
    </div>
  </div>
</div>

<script>
const DATA = __GRAPH_DATA__;

// ── Constants ─────────────────────────────────────────────────────────────
const TYPE_COLORS = {
  project:  '#f0883e',
  decision: '#58a6ff',
  pattern:  '#3fb950',
  note:     '#d2a8ff',
  profile:  '#ff7b72',
  file:     '#20b2aa',
};
const SOURCE_COLORS = {
  agent:     '#58a6ff',
  git:       '#f0883e',
  bootstrap: '#d2a8ff',
  hook:      '#d2a8ff',  // session stop-hook captures; same hue as bootstrap
  explicit:  '#3fb950',
  project:   '#f0883e',
  code:      '#20b2aa',
};
const EDGE_DEFS = [
  { type: 'belongs_to',      label: 'Project membership',    color: '#388bfd', dash: '' },
  { type: 'file_overlap',    label: 'Same files changed',    color: '#f0883e', dash: '4,3' },
  { type: 'temporal',        label: 'Commit order',          color: '#d2a8ff', dash: '6,4' },
  { type: 'keyword_overlap', label: 'Shared keywords',       color: '#3fb950', dash: '4,4' },
  { type: 'cochange',        label: 'Co-changed files',      color: '#20b2aa', dash: '' },
  { type: 'memory_file',     label: 'Commit mentions file',  color: '#20b2aa', dash: '3,4' },
];
const SENTIMENT_COLORS = { positive: '#3fb950', negative: '#ff7b72', mixed: '#d29922' };

// ── State ─────────────────────────────────────────────────────────────────
let activeTypes = new Set(Object.keys(TYPE_COLORS));
let activeSources = new Set(['agent','git','bootstrap','hook','explicit','project','code']);
let activeEdges = new Set(EDGE_DEFS.map(e => e.type));
let activeLayers = new Set(['memories', 'code']);
let selectedId = null;
let searchQuery = '';

// ── Visible sets ──────────────────────────────────────────────────────────
function visibleNodeIds() {
  const q = searchQuery.toLowerCase();
  return new Set(
    DATA.nodes
      .filter(n => {
        const layer = n.type === 'file' ? 'code' : 'memories';
        return activeLayers.has(layer);
      })
      .filter(n => activeTypes.has(n.type) && activeSources.has(n.source || 'explicit'))
      .filter(n => {
        if (!q) return true;
        return (n.label||'').toLowerCase().includes(q)
          || (n.full_content||'').toLowerCase().includes(q)
          || (n.full_path||'').toLowerCase().includes(q)
          || (n.language||'').toLowerCase().includes(q)
          || (n.keywords||[]).some(k => k.toLowerCase().includes(q))
          || (n.git_files||[]).some(f => f.toLowerCase().includes(q))
          || (n.git_author||'').toLowerCase().includes(q);
      })
      .map(n => n.id)
  );
}

// ── SVG / zoom ────────────────────────────────────────────────────────────
const svg = d3.select('#svg');
const hullG = svg.append('g').attr('class', 'hulls');
const linkG = svg.append('g').attr('class', 'links');
const nodeG = svg.append('g').attr('class', 'nodes');
const zoom = d3.zoom().scaleExtent([0.03, 5]).on('zoom', e => {
  hullG.attr('transform', e.transform);
  linkG.attr('transform', e.transform);
  nodeG.attr('transform', e.transform);
});
svg.call(zoom);

// ── Simulation ────────────────────────────────────────────────────────────
const simulation = d3.forceSimulation()
  .force('link', d3.forceLink().id(d => d.id)
    .distance(d => {
      if (d.type === 'belongs_to') return 80;
      if (d.type === 'temporal') return 55;
      if (d.type === 'file_overlap') return 100;
      if (d.type === 'cochange') return 45;
      if (d.type === 'memory_file') return 110;
      return 170;
    })
    .strength(d => {
      if (d.type === 'belongs_to') return 0.9;
      if (d.type === 'temporal') return 0.7;
      if (d.type === 'file_overlap') return 0.5;
      if (d.type === 'cochange') return 0.65;
      if (d.type === 'memory_file') return 0.12;
      return 0.15;
    })
  )
  .force('charge', d3.forceManyBody().strength(d => d.type === 'project' ? -800 : -150))
  .force('center', d3.forceCenter(0, 0))
  .force('collide', d3.forceCollide().radius(d => d.size + 5))
  .alphaDecay(0.022);

// ── Build graph ───────────────────────────────────────────────────────────
let linkSel, nodeSel;

function buildGraph() {
  const visIds = visibleNodeIds();
  const nodes = DATA.nodes.filter(n => visIds.has(n.id));
  const nodeSet = new Set(nodes.map(n => n.id));
  const links = DATA.edges.filter(e => {
    const s = e.source.id || e.source, t = e.target.id || e.target;
    return nodeSet.has(s) && nodeSet.has(t) && activeEdges.has(e.type);
  });

  linkG.selectAll('*').remove();
  nodeG.selectAll('*').remove();
  hullG.selectAll('*').remove();

  // Links
  linkSel = linkG.selectAll('line')
    .data(links, d => `${d.source.id||d.source}-${d.target.id||d.target}-${d.type}`)
    .join('line')
    .attr('class', d => `link ${d.type}`)
    .attr('stroke-width', d => d.type === 'belongs_to' ? 1.5 : 1);

  // Nodes
  const gNode = nodeG.selectAll('g')
    .data(nodes, d => d.id)
    .join('g')
    .attr('class', d => `node ${d.type}${d.source === 'git' ? ' git-node' : ''}`)
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end',   (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; })
    )
    .on('click',     (e, d) => { e.stopPropagation(); selectNode(d.id); })
    .on('mouseover', (e, d) => showTooltip(e, d))
    .on('mousemove', e => moveTooltip(e))
    .on('mouseout',  hideTooltip);

  nodeSel = gNode;

  // Circles for memory/project nodes
  gNode.filter(d => d.type !== 'file')
    .append('circle')
    .attr('r', d => d.size)
    .attr('fill', d => TYPE_COLORS[d.type] || '#8b949e')
    .attr('stroke', d => {
      if (d.source === 'git') return '#f0883e';
      return d3.color(TYPE_COLORS[d.type] || '#8b949e').darker(0.8);
    })
    .attr('stroke-width', d => d.source === 'git' ? 2.5 : 1.5);

  // Diamonds for code file nodes
  gNode.filter(d => d.type === 'file')
    .append('polygon')
    .attr('points', d => {
      const r = d.size;
      return `0,${-r} ${r},0 0,${r} ${-r},0`;
    })
    .attr('fill', TYPE_COLORS.file)
    .attr('stroke', d3.color(TYPE_COLORS.file).darker(0.8))
    .attr('stroke-width', 1.5);

  gNode.filter(d => d.type === 'project')
    .append('text')
    .text(d => d.label)
    .attr('dy', d => d.size + 13);

  simulation.nodes(nodes).on('tick', ticked);
  simulation.force('link').links(links);
  simulation.alpha(0.9).restart();
}

function ticked() {
  if (linkSel) linkSel
    .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
    .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
  if (nodeSel) nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
  drawHulls();
}

// ── Convex hulls per project ──────────────────────────────────────────────
function drawHulls() {
  if (!nodeSel) return;
  // Group visible non-project nodes by project
  const byProject = {};
  nodeSel.each(d => {
    if (d.project) {
      if (!byProject[d.project]) byProject[d.project] = [];
      byProject[d.project].push([d.x, d.y]);
    }
  });
  // Also include the project node itself
  nodeSel.each(d => {
    if (d.type === 'project') {
      const name = d.label;
      if (!byProject[name]) byProject[name] = [];
      byProject[name].push([d.x, d.y]);
    }
  });

  const hullData = Object.entries(byProject)
    .filter(([, pts]) => pts.length >= 3)
    .map(([proj, pts]) => ({ proj, hull: d3.polygonHull(pts) }))
    .filter(d => d.hull);

  hullG.selectAll('path').data(hullData, d => d.proj)
    .join('path')
    .attr('class', 'hull')
    .attr('d', d => 'M' + d.hull.map(p => p.join(',')).join('L') + 'Z')
    .attr('fill', d => {
      // Find project node color
      return TYPE_COLORS['project'];
    })
    .attr('stroke', TYPE_COLORS['project']);
}

// ── Select / fade ─────────────────────────────────────────────────────────
function selectNode(id) {
  selectedId = (selectedId === id) ? null : id;
  updateFade();
  updateDetail();
}

function connectedIds(id) {
  const c = new Set([id]);
  DATA.edges.forEach(e => {
    const s = e.source.id || e.source, t = e.target.id || e.target;
    if (s === id) c.add(t);
    if (t === id) c.add(s);
  });
  return c;
}

function updateFade() {
  if (!selectedId) {
    nodeSel && nodeSel.classed('faded', false).classed('selected', false);
    linkSel && linkSel.classed('faded', false);
    return;
  }
  const conn = connectedIds(selectedId);
  nodeSel && nodeSel
    .classed('faded',    d => !conn.has(d.id))
    .classed('selected', d => d.id === selectedId);
  linkSel && linkSel.classed('faded', d => {
    const s = d.source.id || d.source, t = d.target.id || d.target;
    return !(conn.has(s) && conn.has(t));
  });
}

// ── Detail panel ──────────────────────────────────────────────────────────
function updateDetail() {
  const panel = document.getElementById('detail-panel');
  if (!selectedId) {
    panel.innerHTML = '<div class="placeholder">Click a node to inspect it.</div>';
    return;
  }
  const n = DATA.nodes.find(x => x.id === selectedId);
  if (!n) return;

  const color = TYPE_COLORS[n.type] || '#8b949e';
  let html = '';

  // Type + source badge
  html += `<div id="detail-type-badge" style="background:${color}22;color:${color};border:1px solid ${color}44">`;
  html += `● ${n.type}`;
  if (n.source && n.source !== 'explicit' && n.source !== 'project') {
    const sc = SOURCE_COLORS[n.source] || '#8b949e';
    html += ` &nbsp;<span style="color:${sc};font-weight:400">via ${n.source}</span>`;
  }
  html += '</div>';

  // Code file node
  if (n.type === 'file') {
    html += `<div id="detail-title">${escHtml(n.label)}</div>`;
    html += `<div id="detail-meta">`;
    if (n.project) html += `<span>📁 ${escHtml(n.project)}</span>`;
    if (n.language && n.language !== 'unknown') html += `<span>🔤 ${escHtml(n.language)}</span>`;
    if (n.cochange_score) html += `<span>🔗 ${n.cochange_score} co-changes</span>`;
    html += `</div>`;
    html += `<div id="detail-content">${escHtml(n.full_path || n.label)}</div>`;
  }

  // Git commit card / standard memory (skip for file nodes)
  if (n.type !== 'file') {
    if (n.git_hash) {
      html += `<div class="git-card">`;
      html += `<div class="git-hash">⬡ ${n.git_hash}</div>`;
      html += `<div class="git-row"><strong>${escHtml(n.label)}</strong></div>`;
      if (n.git_date) html += `<div class="git-row">📅 <strong>${n.git_date}</strong></div>`;
      if (n.git_author) html += `<div class="git-row">👤 <strong>${escHtml(n.git_author)}</strong></div>`;
      if (n.git_files_changed) {
        html += `<div class="git-stat">`;
        html += `<span>${n.git_files_changed} files</span>`;
        html += `<span class="git-add">+${n.git_additions}</span>`;
        html += `<span class="git-del">−${n.git_deletions}</span>`;
        html += `</div>`;
      }
      if (n.git_files && n.git_files.length) {
        html += `<div class="git-files">`;
        n.git_files.forEach(f => { html += `<span class="git-file-tag">${escHtml(f)}</span>`; });
        html += `</div>`;
      }
      html += `</div>`;
    } else {
      // Standard memory
      html += `<div id="detail-title">${escHtml(n.full_content || n.label)}</div>`;
    }

    // Meta row
    html += `<div id="detail-meta">`;
    if (n.project) html += `<span>📁 ${escHtml(n.project)}</span>`;
    if (n.created_at) html += `<span>🗓 ${n.created_at}</span>`;
    if (n.access_count) html += `<span>🔥 ${n.access_count}×</span>`;
    html += `</div>`;
  }

  // Outcome
  if (n.outcome) {
    const sc = SENTIMENT_COLORS[n.outcome_sentiment] || '#8b949e';
    html += `<div id="detail-outcome" style="background:#0d1117;border:1px solid ${sc}44;border-radius:6px;padding:8px 10px;margin-bottom:8px;font-size:11px">`;
    if (n.outcome_sentiment) html += `<span style="color:${sc};font-weight:600">${n.outcome_sentiment.toUpperCase()} · </span>`;
    html += escHtml(n.outcome) + '</div>';
  }

  // Keywords
  if (n.keywords && n.keywords.length) {
    html += `<div id="detail-keywords">`;
    n.keywords.slice(0, 8).forEach(k => { html += `<span>${escHtml(k)}</span>`; });
    html += `</div>`;
  }

  // Connected nodes grouped by edge type
  const edgeGroups = {};
  DATA.edges.forEach(e => {
    const s = e.source.id || e.source, t = e.target.id || e.target;
    let other = null;
    if (s === selectedId) other = t;
    if (t === selectedId) other = s;
    if (other) {
      if (!edgeGroups[e.type]) edgeGroups[e.type] = [];
      edgeGroups[e.type].push({ id: other, edgeData: e });
    }
  });

  const edgeTypeLabels = {
    belongs_to: '📁 Project',
    keyword_overlap: '🔤 Shared keywords',
    file_overlap: '📄 Same files changed',
    temporal: '⏱ Adjacent commit',
    cochange: '🔗 Co-changed with',
    memory_file: '💾 Referenced by commit',
  };

  const totalConn = Object.values(edgeGroups).reduce((s, a) => s + a.length, 0);
  if (totalConn) {
    html += `<div style="margin-top:12px;font-size:11px;color:#8b949e;margin-bottom:4px">Connected (${totalConn}):</div>`;
    Object.entries(edgeGroups).forEach(([type, items]) => {
      const label = edgeTypeLabels[type] || type;
      html += `<div style="font-size:10px;color:#8b949e;margin:6px 0 3px">${label}</div>`;
      items.slice(0, 5).forEach(({ id: cid, edgeData }) => {
        const cn = DATA.nodes.find(x => x.id === cid);
        if (!cn) return;
        const cc = TYPE_COLORS[cn.type] || '#8b949e';
        const snippet = ((cn.git_hash ? cn.label : cn.full_content) || cn.label).substring(0, 60);
        const extra = type === 'keyword_overlap' && edgeData.shared_keywords
          ? ` <span style="color:#3fb950">[${edgeData.shared_keywords.slice(0,3).join(', ')}]</span>` : '';
        const fileExtra = type === 'file_overlap' && edgeData.shared_files
          ? ` <span style="color:#f0883e">[${edgeData.shared_files.slice(0,2).join(', ')}]</span>` : '';
        html += `<div class="connected-node" onclick="selectNode('${cid}')">`;
        html += `<span style="color:${cc}">●</span> ${escHtml(snippet)}…${extra}${fileExtra}</div>`;
      });
    });
  }

  panel.innerHTML = html;
}

svg.on('click', () => { selectedId = null; updateFade(); updateDetail(); });

// ── Tooltip ───────────────────────────────────────────────────────────────
const tooltip = document.getElementById('tooltip');
function showTooltip(e, d) {
  const color = TYPE_COLORS[d.type] || '#8b949e';
  let html = `<div class="tt-type" style="color:${color}">● ${d.type}${d.project ? ' · ' + d.project : ''}`;
  if (d.source === 'git') html += ' · <span style="color:#f0883e">git</span>';
  html += `</div>`;
  if (d.type === 'file') {
    html += `<div class="tt-content">${escHtml(d.full_path || d.label)}</div>`;
    const meta = [];
    if (d.language && d.language !== 'unknown') meta.push(d.language);
    if (d.cochange_score) meta.push(`${d.cochange_score} co-changes`);
    if (meta.length) html += `<div class="tt-files">${meta.join(' · ')}</div>`;
  } else if (d.git_hash) {
    html += `<div class="tt-git">⬡ ${d.git_hash} · ${d.git_date || ''}</div>`;
    html += `<div class="tt-content">${escHtml(d.label.substring(0, 100))}</div>`;
    if (d.git_files && d.git_files.length)
      html += `<div class="tt-files">${d.git_files.slice(0,3).join(' · ')}</div>`;
  } else {
    html += `<div class="tt-content">${escHtml((d.full_content || d.label).substring(0, 130))}${(d.full_content||d.label).length > 130 ? '…' : ''}</div>`;
  }
  tooltip.innerHTML = html;
  tooltip.style.display = 'block';
  moveTooltip(e);
}
function moveTooltip(e) { tooltip.style.left = (e.clientX+14)+'px'; tooltip.style.top = (e.clientY-10)+'px'; }
function hideTooltip() { tooltip.style.display = 'none'; }

// ── Filter chips — layer ──────────────────────────────────────────────────
const chipsLayerEl = document.getElementById('chips-layer');
const layerDefs = [
  { key: 'memories', label: 'Memories', color: '#58a6ff' },
  { key: 'code',     label: 'Code files', color: '#20b2aa' },
];
layerDefs.forEach(({ key, label, color }) => {
  const hasNodes = DATA.nodes.some(n => (key === 'code' ? n.type === 'file' : n.type !== 'file' && n.type !== 'project'));
  if (!hasNodes && key === 'code') return;  // hide if no code nodes indexed
  const chip = document.createElement('div');
  chip.className = 'chip';
  chip.style.cssText = `background:${color}18;border-color:${color};color:${color}`;
  chip.textContent = label;
  chip.addEventListener('click', () => {
    if (activeLayers.has(key)) activeLayers.delete(key); else activeLayers.add(key);
    chip.classList.toggle('inactive', !activeLayers.has(key));
    buildGraph();
  });
  chipsLayerEl.appendChild(chip);
});

// ── Filter chips — type ───────────────────────────────────────────────────
const chipsTypeEl = document.getElementById('chips-type');
Object.entries(TYPE_COLORS).forEach(([type, color]) => {
  const count = DATA.stats.by_type[type] || 0;
  if (!count) return;
  const chip = document.createElement('div');
  chip.className = 'chip';
  chip.style.cssText = `background:${color}18;border-color:${color};color:${color}`;
  chip.textContent = `${type} (${count})`;
  chip.addEventListener('click', () => {
    if (activeTypes.has(type)) activeTypes.delete(type); else activeTypes.add(type);
    chip.classList.toggle('inactive', !activeTypes.has(type));
    buildGraph();
  });
  chipsTypeEl.appendChild(chip);
});

// ── Filter chips — source ─────────────────────────────────────────────────
const chipsSourceEl = document.getElementById('chips-source');
Object.entries(DATA.stats.by_source || {}).forEach(([src, count]) => {
  if (!count || src === 'project') return;
  const color = SOURCE_COLORS[src] || '#8b949e';
  const chip = document.createElement('div');
  chip.className = 'chip';
  chip.style.cssText = `background:${color}18;border-color:${color};color:${color}`;
  chip.textContent = `${src} (${count})`;
  chip.addEventListener('click', () => {
    if (activeSources.has(src)) activeSources.delete(src); else activeSources.add(src);
    chip.classList.toggle('inactive', !activeSources.has(src));
    buildGraph();
  });
  chipsSourceEl.appendChild(chip);
});

// ── Legend ────────────────────────────────────────────────────────────────
const legendEl = document.getElementById('legend-edges');
EDGE_DEFS.forEach(def => {
  const item = document.createElement('div');
  item.className = 'legend-item';
  item.innerHTML = `<div class="legend-line" style="background:${def.color};${def.dash ? `background:none;border-top:2px dashed ${def.color}` : ''}"></div>${def.label}`;
  legendEl.appendChild(item);
});

// ── Edge toggles ──────────────────────────────────────────────────────────
const edgeToggleEl = document.getElementById('edge-toggles');
EDGE_DEFS.forEach(def => {
  const row = document.createElement('div');
  row.className = 'et-row';
  row.innerHTML = `<div class="et-swatch" style="${def.dash ? `border-top:2px dashed ${def.color}` : `background:${def.color};height:2px`}"></div><div class="et-label">${def.label}</div>`;
  row.addEventListener('click', () => {
    if (activeEdges.has(def.type)) activeEdges.delete(def.type); else activeEdges.add(def.type);
    row.classList.toggle('inactive', !activeEdges.has(def.type));
    buildGraph();
  });
  edgeToggleEl.appendChild(row);
});

// ── Header stats ──────────────────────────────────────────────────────────
const st = DATA.stats;
const gitCount = st.by_source?.git || 0;
const fileCount = st.by_type?.file || 0;
let statsText = `${st.total_nodes} nodes · ${st.total_edges} edges`;
if (gitCount) statsText += ` · ${gitCount} git commits`;
if (fileCount) statsText += ` · ${fileCount} code files`;
document.getElementById('header-stats').textContent = statsText;

// ── Zoom controls ─────────────────────────────────────────────────────────
document.getElementById('btn-zoom-in').addEventListener('click',  () => svg.transition().call(zoom.scaleBy, 1.4));
document.getElementById('btn-zoom-out').addEventListener('click', () => svg.transition().call(zoom.scaleBy, 0.7));
document.getElementById('btn-reset').addEventListener('click',    () => svg.transition().call(zoom.transform, d3.zoomIdentity));

// ── Util ──────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
window.selectNode = selectNode;

// ── Initial render ────────────────────────────────────────────────────────
buildGraph();

// Auto-fit after simulation settles
setTimeout(() => {
  const allNodes = nodeG.node();
  if (!allNodes) return;
  const b = allNodes.getBBox();
  const svgEl = document.getElementById('svg');
  const w = svgEl.clientWidth, h = svgEl.clientHeight;
  if (b.width > 0) {
    const scale = 0.75 * Math.min(w / b.width, h / b.height);
    const tx = w/2 - scale*(b.x + b.width/2);
    const ty = h/2 - scale*(b.y + b.height/2);
    svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
  }
}, 2800);
</script>
</body>
</html>
"""


def render_html(data: dict) -> str:
    # Escape </script> sequences so memory content cannot break out of the
    # <script> block.  json.dumps does not escape forward slashes by default.
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__GRAPH_DATA__", data_json)


def generate(output_path: Path) -> Path:
    db.init_db()
    data = build_graph_data()
    html = render_html(data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path

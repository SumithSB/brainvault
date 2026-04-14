# Changelog

All notable changes to brainvault will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Security

- **Graph HTML (`graph`)**: removed unsafe inline `onclick` handlers; connected-node navigation uses DOM listeners; tooltips and badges escape dynamic text consistently
- **Installer**: invalid `~/.claude/settings.json` no longer causes a destructive rewrite — parse failures abort install after writing a timestamped backup copy
- **Hooks**: PostToolUse stdin capped at 256 KiB; best-effort redaction of common secret patterns in Bash summaries before persisting to `session_events`
- **CI / publish workflows**: third-party GitHub Actions pinned to full commit SHAs

### Fixed

- **SQLite**: connection `timeout`, `PRAGMA busy_timeout`, `PRAGMA foreign_keys=ON`; `update_memory` uses a single transaction and respects `rowcount` when a row disappears between read and write
- **Stop hook / capture**: broad silent failures now log one-line diagnostics to stderr
- **Git seeding (install)**: per-repo scan failures print a short message to stderr instead of failing silently

### Added
- `brainvault/py.typed` marker (PEP 561)
- `SECURITY.md` and `CODE_OF_CONDUCT.md`
- Tests: `tests/test_graph.py`, installer settings JSON tests in `tests/test_new_features.py`
- **One global install** covers all existing and future projects — patches `~/.claude/settings.json` and `~/.claude/CLAUDE.md`, not anything project-specific
- **Fully automatic after install** — git scan, first-time repo indexing, daily re-index, and embedding backfill all run from the Stop hook with no manual steps; only repos >5 000 source files require explicit `index-repo`
- **Session replay** (`tool_capture.py`): PostToolUse hook captures every Write/Edit/Bash/TodoWrite/NotebookEdit call as a compact event row; builds a per-session timeline without slowing Claude Code (<20 ms per event, no reads, no embedding)
- **`session_events` table** in `db.py`: ring buffer storing session activity; pruned at 90 days; new functions `record_tool_event`, `get_session_timeline`, `get_recent_activity`, `prune_old_events`
- **`get_recent_activity` MCP tool** (11th tool): compact index of recent sessions — event counts, tools used, first/last timestamps; tiered retrieval so Claude loads only what it needs
- **`get_session_timeline` MCP tool** (12th tool): full chronological event list for a specific session ID
- **`sessions` CLI command**: list recent sessions with event counts and tool breakdown
- **`activity` CLI command**: show full event timeline for a session ID
- **PostToolUse hook** registered by `brainvault install`: matcher `"Write|Edit|Bash|TodoWrite|NotebookEdit"` filters noise at the Claude Code level; only action tools trigger `brainvault.tool_capture`
- **Code intelligence** (`code_scan.py`): `index-repo` command indexes file structure, detects language, extracts imports, and builds a co-change matrix from git history (files that change together)
- **`get_code_context` MCP tool** (10th tool): returns ranked relevant files + co-change partners + memories before starting feature work — reduces exploratory file reads
- **`graph` command** (`graph.py`): generates a self-contained D3.js force-directed HTML brain graph; two-layer visualization — memory nodes (circles, 5 types) + code file nodes (teal diamonds from `index-repo`); 6 edge types including `cochange` (teal solid, files that change together) and `memory_file` (teal dashed, git commit → file it touched); Layer filter chips toggle Memories and Code files independently; search spans file paths, languages, keywords, authors; co-change edges pull highly-coupled files into tight clusters
- **`git-scan` command** (`git_scan.py`): mines git history for significant commits (refactors, migrations, large diffs) and saves them as decision/pattern/note memories tagged `source="git"`
- **`bootstrap-git` command**: discovers all git repos under a path (default `~/`) and runs git-scan on each
- **`status` command**: vault health at a glance — memory counts by type/source, unembedded count, open decisions, stale projects, last session
- **`update` command**: edit an existing memory's content, type, or project by ID
- **`reflect` command**: surfaces open decisions (>7 days old, no outcome), cross-project keyword patterns, outcome sentiment breakdown, stale projects, hot memories
- **`forget` command**: delete a memory by ID from CLI
- **`init` command**: structured onboarding for a new project (name, stack, goals, constraints, alternatives)
- **`index-repo` command**: indexes file structure and co-change matrix for a single repo
- **`embed` command**: backfill semantic embeddings for all memories lacking a vector
- **`record_outcome`** MCP tool: close the feedback loop on a past decision with outcome text and sentiment (positive/negative/mixed)
- **`update_memory`** MCP tool: edit an existing memory in place
- **`reflect`** MCP tool: cross-project gap analysis
- **Semantic search** (`embeddings.py`): optional fastembed + sqlite-vec integration; Reciprocal Rank Fusion blends BM25 (FTS5) and cosine similarity rankings
- **`VALID_MEMORY_TYPES`** constant in `db.py`: single source of truth used by CLI, MCP server, and DB CHECK constraint
- Auto-seed on install: `brainvault install` now automatically scans all local git repos and imports past Claude Code session summaries — vault is pre-seeded before first use (Ctrl+C to skip)
- Smart CLAUDE.md upgrades: re-running `install` replaces the managed block in-place using start/end markers, preserving surrounding user content

### Changed
- **Graph layout**: keyword and git file-overlap edges use inverted-index candidate pairs (scales better on large vaults)
- **Packaging**: PyPI classifier `Development Status :: 4 - Beta`; `Typing :: Typed`
- `installer.py`: post-install seeding is now automatic (no y/N prompts); TTY-aware progress output
- `_migrate()` in `db.py`: all schema changes are additive; new indexes on `memories(source)` and `memories(project, created_at)` added for query performance
- `_extract_keywords()`: capped at first 5 000 chars to avoid scanning huge memory blobs
- `except (ImportError, Exception)` in `get_connection()` narrowed to `(ImportError, AttributeError, sqlite3.OperationalError)` — no longer swallows unexpected errors
- Python import regex: fixed `[\w.,\s]+` → `[\w., \t]+` so newlines don't bleed across import statements

### Removed
- `click` and `rich` removed from `dependencies` in `pyproject.toml` — were listed but never imported

---

## [0.1.0] - 2026-04-09

### Added
- SQLite + FTS5 storage layer with full-text search (`db.py`)
- MCP server exposing 6 tools: `get_my_context`, `save_memory`, `search_memory`, `register_project`, `get_project`, `forget` (`mcp_server.py`)
- Stop hook handler that auto-captures Claude-generated continuation summaries after each session (`capture.py`)
- Bootstrap importer that seeds memory from all existing Claude Code session history (`bootstrap.py`)
- CLI with `install`, `bootstrap`, `search`, and `stats` commands (`cli.py`)
- One-command setup: patches `~/.claude/settings.json` and `~/.claude/CLAUDE.md` (`installer.py`)
- Full test suite (29 tests) covering DB layer and MCP tools

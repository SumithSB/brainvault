# Changelog

All notable changes to brainvault will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.2.5] - 2026-04-18

### Changed

- **CONTRIBUTING.md** — Expanded *PyPI README images* (public repo / absolute URLs, PNG vs SVG).
- **Assets** — Updated `assets/logo.png` and `assets/logo.svg` (refined artwork; PyPI README still references `logo.png` via `raw.githubusercontent.com`).

## [0.2.4] - 2026-04-18

### Fixed

- **README / PyPI** — Hero logo `<img>` uses an absolute `https://raw.githubusercontent.com/SumithSB/brainvault/main/assets/logo.png` URL so the image renders on **pypi.org** (relative `assets/...` paths do not). With the GitHub repository **public**, that URL is anonymously reachable and the logo displays on both GitHub and PyPI.

## [0.2.3] - 2026-04-18

### Fixed

- **PyPI project page** — README hero logo uses an **absolute** `https://raw.githubusercontent.com/.../assets/logo.png` URL. Relative paths like `assets/logo.png` do not work on PyPI (the browser resolves them against `pypi.org`). The logo displays on PyPI when that URL is **publicly reachable** (repository public, or logo hosted on another public HTTPS URL).

### Changed

- **CONTRIBUTING.md** — Document PyPI README images: absolute URLs, PNG/JPEG preferred over SVG, private-repo caveats.
- **Assets** — Updated `assets/logo.png` / `assets/logo.svg`.

## [0.2.2] - 2026-04-18

### Fixed

- **PyPI project page** — README hero image now points at `assets/logo.png` (raster). Warehouse’s long-description sanitizer typically strips remote **SVG** images; PNG displays reliably. `assets/logo.svg` remains the source vector for other uses.

## [0.2.1] - 2026-04-18

### Fixed

- **Windows** — Adapter instruction and config files (`CLAUDE.md`, Cursor rules, JSON) are read/written with **UTF-8** so Unicode in the managed block (e.g. arrows, dashes) does not raise `UnicodeEncodeError` under cp1252.
- **Windows / tests** — Claude and Cursor config paths resolve **`Path.home()` at access time** (`_HomeRelativePath`) so `USERPROFILE` / `HOME` overrides in tests and CI apply; `bootstrap.claude_projects_dir()` follows `ClaudeCodeAdapter.SESSIONS_PATH`.
- **`code_scan.scan_file_tree`** — Relative `file_path` values use **POSIX separators** (`as_posix()`) for stable cross-platform behavior.
- **CLI** — `export` / `import` file I/O uses UTF-8.
- **Tests** — Fixture `read_text` / `write_text` for instruction files uses UTF-8 on Windows.

### Changed

- **Formatting** — `ruff format` applied to `brainvault/adapters/cursor.py`.

### Fixed (CI)

- **Windows CI** — `tests/test_code_scan.py` import-extraction tests no longer write under POSIX-only `/tmp` (use `tmp_path` so `C:\\tmp` is not required on Windows runners).

## [0.2.0] - 2026-04-18

### Added

- **PyPI project metadata** — `readme` in `pyproject.toml` (long description from `README.md`); `Programming Language :: Python :: 3.13` classifier; `build` and `twine` in `[project.optional-dependencies] dev` for local `python -m build` / `python -m twine check dist/*`.
- **Documentation** — README: scope vs other agent-memory and graph-visualization approaches, roadmap (next releases), and session-start wording aligned with actual `get_my_context()` output.
- **Cursor support** — `brainvault install` patches both Claude Code and Cursor when either is detected. Cursor integration writes `~/.cursor/mcp.json`, `~/.cursor/rules/brainvault.mdc`, and `~/.cursor/hooks.json` (`stop`, scoped `postToolUse`, `afterFileEdit`, `afterShellExecution`) calling `brainvault.capture` / `brainvault.tool_capture`. Session notes from `~/.cursor/projects/*/agent-transcripts/*/*.jsonl` and tool replay rows are tagged `source_agent='cursor'`.
- **`AgentAdapter` abstraction** (`brainvault/adapters/`): one adapter per host (`ClaudeCodeAdapter`, `CursorAdapter`). Rest of brainvault is host-agnostic — `installer.py`, `doctor`, and CLI dispatch over `resolve(agents)` / `installed_adapters()` helpers. `--agent claude_code` / `--agent cursor` / `--agent all` flags on `install` + `uninstall` for explicit targeting.
- **`source_agent` column** on `memories`, `session_events`, `sessions_captured` (additive migration, defaults `'claude_code'` to backfill existing rows). Lets future queries/graphs split memory by host.
- **`uninstall` command** — cleanly reverses `install`: strips `mcpServers.brainvault`, removes the brainvault Stop + PostToolUse hook entries (preserving unrelated entries), and deletes the managed block from `~/.claude/CLAUDE.md`. Pass `--purge` (with `--yes` for non-interactive) to also delete `~/.brainvault/`.
- **`doctor` command** — diagnose install health: DB integrity + FTS5 check, adapter-contributed checks (per-host MCP entry + hooks + instruction block markers), `brainvault.mcp_server` importable, optional semantic stack, git on PATH. Non-zero exit when any check fails.
- **`export` command** — dump memories + projects as JSON (default, schema-versioned) or Markdown. Supports `--project <name>` for single-project export and `--include-events` to also dump the session replay buffer.
- **`import` command** — restore from a JSON export. Merges by default (skips IDs that already exist); `--replace` overwrites colliding rows. Rejects future schema versions with a clear error.
- **CI matrix**: `ubuntu-latest`, `macos-latest`, `windows-latest` × Python 3.10/3.12 (3.11 kept on ubuntu only) so macOS- and Windows-specific path issues surface in CI.
- **`VALID_SOURCE_AGENTS` / `SYSTEM_SOURCE_AGENT`** in `db.py` — validates `source_agent` on `save_memory`, `record_tool_event`, and `mark_session_captured`.
- **Windows hook smoke** — `tests/test_windows_hook_smoke.py` (skipped on non-Windows) runs the Claude Stop hook command under the platform shell.
- `brainvault/py.typed` marker (PEP 561)
- `SECURITY.md` and `CODE_OF_CONDUCT.md`
- Tests: `tests/test_graph.py`, installer settings JSON tests in `tests/test_new_features.py`
- **One global install** covers all existing and future projects — patches `~/.claude/settings.json` and `~/.claude/CLAUDE.md`, not anything project-specific
- **Fully automatic after install** — git scan, first-time repo indexing, daily re-index, and embedding backfill all run from the Stop hook with no manual steps; only repos >5 000 source files require explicit `index-repo`
- **Session replay** (`tool_capture.py`): PostToolUse hook captures every Write/Edit/Bash/TodoWrite/NotebookEdit call as a compact event row; builds a per-session timeline without slowing Claude Code (<20 ms per event, no reads, no embedding)
- **`session_events` table** in `db.py`: ring buffer storing session activity; `prune_old_events` + Stop hook (`capture`) enforce 90-day retention; new functions `record_tool_event`, `get_session_timeline`, `get_recent_activity`, `prune_old_events`
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

- **README assets** — logo and graph screenshot use `raw.githubusercontent.com` URLs so the PyPI project page renders images correctly.
- **CI** — workflow runs on `v*.*.*` tags as well as `main`, so releases are tested the same way as branch pushes.
- **Publish workflow** — runs `pytest` and `twine check dist/*` before uploading to PyPI.
- **sdist contents** — Hatch excludes `/.claude` and `/uv.lock` from the source tarball so local agent config and the dev lockfile are not published to PyPI.
- **`get_recent_activity` MCP docstring** — describes cross-host session replay (not Claude Code–only).
- **Token footprint (MCP + instructions)** — Shorter managed Brainvault block in `adapters/claude_code.py` (`INSTRUCTIONS_BODY`, also injected into Cursor rules). MCP tools: `search_memory` truncates each hit’s body (default `max_chars=400`, suffix `… (id: …)`); `get_project` lists at most 20 memories (newest first) with a footer pointing to `search_memory` for more; `get_session_timeline` returns the last 50 events by default with a footer for older rows (`limit` overridable).
- **Adapter owns host-specific logic** — `capture.py` iterates all adapters for recent transcripts; `tool_capture.py` routes stdin JSON via `owns_payload` / `event_from_payload` on `ClaudeCodeAdapter` then `CursorAdapter`. PostToolUse summarisers live in `adapters/claude_code.py`; shared redaction in `adapters/_redact.py`. Legacy `installer` `_patch_*` shims removed; `HookResult` gains `removed` for unregister.
- **`source_agent` populated** at capture/bootstrap/git-scan/MCP boundaries; MCP server reads `BRAINVAULT_SOURCE_AGENT` from the MCP config `env` set by each adapter’s `_mcp_entry`.
- **`install` auto-seed** runs only when `total_memories == 0`.
- **`CursorAdapter.is_installed`** — requires a real marker (`mcp.json`, `extensions`, `settings.json`, `User`, or `rules`) under `~/.cursor/`, not an empty directory.
- **Graph layout**: keyword and git file-overlap edges use inverted-index candidate pairs (scales better on large vaults)
- **Packaging**: PyPI classifier `Development Status :: 4 - Beta`; `Typing :: Typed`
- `installer.py`: post-install seeding is now automatic (no y/N prompts); TTY-aware progress output
- `_migrate()` in `db.py`: all schema changes are additive; new indexes on `memories(source)` and `memories(project, created_at)` added for query performance
- `_extract_keywords()`: capped at first 5 000 chars to avoid scanning huge memory blobs
- `except (ImportError, Exception)` in `get_connection()` narrowed to `(ImportError, AttributeError, sqlite3.OperationalError)` — no longer swallows unexpected errors
- Python import regex: fixed `[\w.,\s]+` → `[\w., \t]+` so newlines don't bleed across import statements

### Security

- **Graph HTML (`graph`)**: removed unsafe inline `onclick` handlers; connected-node navigation uses DOM listeners; tooltips and badges escape dynamic text consistently
- **Installer**: invalid `~/.claude/settings.json` no longer causes a destructive rewrite — parse failures abort install after writing a timestamped backup copy
- **Hooks**: PostToolUse stdin capped at 256 KiB; best-effort redaction of common secret patterns in Bash summaries before persisting to `session_events`
- **CI / publish workflows**: third-party GitHub Actions pinned to full commit SHAs

### Fixed

- **Session replay retention** — each Stop hook run (`brainvault.capture`) calls `db.prune_old_events(90)` so `session_events` older than 90 days are removed, matching documented behaviour.
- **SQLite**: connection `timeout`, `PRAGMA busy_timeout`, `PRAGMA foreign_keys=ON`; `update_memory` uses a single transaction and respects `rowcount` when a row disappears between read and write
- **Stop hook / capture**: broad silent failures now log one-line diagnostics to stderr
- **Git seeding (install)**: per-repo scan failures print a short message to stderr instead of failing silently

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

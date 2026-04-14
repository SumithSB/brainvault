# Contributing to Brainvault

Thanks for your interest in contributing.

---

## Development setup

```bash
git clone https://github.com/SumithSB/brainvault
cd brainvault
pip install -e ".[dev]"
```

---

## Running tests

```bash
pytest
```

Tests use a temporary SQLite database — your `~/.brainvault/memory.db` is never touched. The `mock_embeddings` fixture in `conftest.py` patches fastembed so no model download is required.

---

## Linting and formatting

```bash
ruff check .          # lint
ruff format .         # format
ruff format --check . # check formatting without modifying
```

CI will fail if either check fails.

---

## Project structure

```
brainvault/
├── db.py           — SQLite schema, CRUD, FTS5 search, vector search, reflection queries
│                     VALID_MEMORY_TYPES constant; all migrations in _migrate()
├── mcp_server.py   — 12 MCP tools served via stdio (FastMCP)
├── capture.py      — Stop hook handler; extracts continuation summaries from JSONL session files
├── tool_capture.py — PostToolUse hook handler; records Write/Edit/Bash/TodoWrite/NotebookEdit
│                     events into session_events table; <20 ms per event, never crashes
├── bootstrap.py    — Imports past Claude Code session history into the vault
├── installer.py    — Patches ~/.claude/settings.json + CLAUDE.md; registers Stop + PostToolUse
│                     hooks; auto-seeds vault on install
├── git_scan.py     — Mines git history for significant commits; discover_repos() for system scan
├── code_scan.py    — File tree walker, regex import extractor, co-change matrix builder
├── embeddings.py   — Optional fastembed wrapper (BAAI/bge-small-en-v1.5, lazy singleton)
├── graph.py        — Generates self-contained D3.js HTML brain graph; two-layer
│                     visualization: memory nodes (circles) + code file nodes (teal diamonds)
│                     when code_entities/code_cochange tables are populated by index-repo;
│                     6 edge types: belongs_to, file_overlap, temporal, keyword_overlap,
│                     cochange, memory_file
└── cli.py          — CLI entry point (16 commands, manual sys.argv parsing)

tests/
├── conftest.py          — autouse fixtures: mock_embeddings (no model download), tmp_db (isolated DB)
├── test_db.py           — DB layer, FTS5 search, VALID_MEMORY_TYPES, FTS5 fallback
├── test_mcp.py          — All 12 MCP tools
├── test_capture.py      — Session summary extraction, project name derivation
├── test_bootstrap.py    — Historical session importer
├── test_git_scan.py     — Git history miner, significance filtering, CLI dispatch
├── test_code_scan.py    — File tree walker, import extraction (Python/JS/TS/Go/Dart), co-change matrix
├── test_tool_capture.py — PostToolUse hook handler, session replay DB functions
├── test_new_features.py — status, update, reflect, forget commands; CLAUDE.md upgrade paths
└── test_vector.py       — Semantic search, RRF merge, embedding backfill
```

---

## Key constraints

- No Docker, no external services — SQLite only
- Python 3.10+ compatibility required (`match` statements and `X | Y` union types are fine, `Path.walk()` is not — use `_walk()` in code_scan.py)
- Never write to `~/.brainvault/memory.db` in tests — always use the `tmp_db` fixture
- Schema changes must be additive — add columns/tables in `_migrate()`, never drop
- Subprocess calls must never use `shell=True` — always pass an args list

---

## Adding a new CLI command

1. Add the function `_cmd_<name>()` in `cli.py`
2. Add the dispatch branch in `main()`
3. Add the usage line in `_print_usage()`
4. Update `CLAUDE.md` CLI commands section and `README.md` commands table

## Adding a new MCP tool

1. Decorate with `@mcp.tool()` in `mcp_server.py`
2. Validate `memory_type` against `db.VALID_MEMORY_TYPES` if applicable
3. Update the MCP tools table in `README.md` and `CLAUDE.md`

## Adding a DB schema change

1. Add a migration block in `_migrate()` in `db.py` — check for table/column existence first
2. Never drop columns or tables (existing DBs must survive upgrades)
3. Add tests that exercise the new schema

---

## Submitting a pull request

1. Fork the repo and create a branch from `main`
2. Make your changes with tests
3. Ensure `pytest` and `ruff check .` both pass
4. Update `CHANGELOG.md` under `[Unreleased]`
5. Open a PR — describe what you changed and why

---

## Security issues

Do not file public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).

## Reporting bugs

Open a GitHub issue. Include:
- Python version (`python --version`)
- Brainvault version (`pip show brainvault`)
- OS (macOS / Linux / Windows)
- Steps to reproduce
- What you expected vs. what happened

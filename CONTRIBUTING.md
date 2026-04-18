# Contributing to Brainvault

Thanks for your interest in contributing.

---

## Development setup

```bash
git clone https://github.com/SumithSB/brainvault
cd brainvault
pip install -e ".[dev]"
```

### Local PyPI build check (maintainers / before tagging)

```bash
pip install -e ".[dev]"   # includes build + twine
python -m build
python -m twine check dist/*
```

Fix any `twine check` warnings before pushing a release tag.

The **sdist** tarball intentionally omits `/.claude` and `/uv.lock` (see `pyproject.toml` → `[tool.hatch.build.targets.sdist]`) so local Claude config and the dev lockfile are never published; the **wheel** contains only the `brainvault` package plus `LICENSE` metadata.

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
├── installer.py    — Dispatches install / uninstall over every detected adapter;
│                     auto-seeds vault on install
├── adapters/       — One concrete adapter per host: AgentAdapter ABC in base.py,
│                     ClaudeCodeAdapter (settings.json + CLAUDE.md + hooks + transcripts),
│                     CursorAdapter (mcp.json + rules + hooks.json + transcripts)
├── git_scan.py     — Mines git history for significant commits; discover_repos() for system scan
├── code_scan.py    — File tree walker, regex import extractor, co-change matrix builder
├── embeddings.py   — Optional fastembed wrapper (BAAI/bge-small-en-v1.5, lazy singleton)
├── graph.py        — Generates self-contained D3.js HTML brain graph; two-layer
│                     visualization: memory nodes (circles) + code file nodes (teal diamonds)
│                     when code_entities/code_cochange tables are populated by index-repo;
│                     6 edge types: belongs_to, file_overlap, temporal, keyword_overlap,
│                     cochange, memory_file
└── cli.py          — CLI entry point (20 commands, manual sys.argv parsing)

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

## Publishing to PyPI (maintainers)

Releases are built and uploaded by [`.github/workflows/publish.yml`](.github/workflows/publish.yml) when you push a **git tag** matching `v*.*.*` (for example `v0.2.0`).

### One-time PyPI setup (trusted publishing)

1. Create the **`brainvault`** project on [PyPI](https://pypi.org/) (or claim the name if unused).
2. In PyPI → **Your project** → **Publishing** → **Add a new pending publisher** (trusted publishing):
   - **PyPI Project Name:** `brainvault`
   - **Owner:** `SumithSB` (GitHub org or user that owns the repo)
   - **Repository name:** `brainvault`
   - **Workflow name:** `publish.yml`
   - **Environment name:** leave **empty** unless you intentionally restrict uploads to a [GitHub Environment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment); if you set one on PyPI, add a matching `environment:` block to the **publish** job in `publish.yml`.
3. Save the pending publisher on PyPI; the next successful workflow run from that repo/workflow can complete the link.

Details: [PyPI trusted publishers](https://docs.pypi.org/trusted-publishers/).

### Release checklist

1. Ensure `main` passes CI (`pytest`, `ruff`).
2. Bump **`version`** in [`pyproject.toml`](pyproject.toml) and add a **`[x.y.z]`** section with date in [`CHANGELOG.md`](CHANGELOG.md) (move notable items out of `[Unreleased]` as appropriate).
3. Run locally: `python -m build` and `twine check dist/*`.
4. Tag and push: `git tag vX.Y.Z` then `git push origin vX.Y.Z`.
5. Confirm the **Publish to PyPI** workflow completes; verify [pypi.org/project/brainvault](https://pypi.org/project/brainvault/).

`workflow_dispatch` on the publish workflow builds artifacts only; upload runs on **tag push** (`v*.*.*`) so releases stay explicit.

## Security issues

Do not file public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).

## Reporting bugs

Open a GitHub issue. Include:
- Python version (`python --version`)
- Brainvault version (`pip show brainvault`)
- OS (macOS / Linux / Windows)
- Steps to reproduce
- What you expected vs. what happened

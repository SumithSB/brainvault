# Contributing to Brainvault

Thanks for your interest in contributing.

---

## Development setup

```bash
git clone https://github.com/sumithsb/brainvault
cd brainvault
pip install -e ".[dev]"
```

---

## Running tests

```bash
pytest
```

Tests use a temporary SQLite database — your `~/.brainvault/memory.db` is never touched.

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
├── db.py           — SQLite schema, CRUD, FTS5 search
├── mcp_server.py   — MCP tools (the core interface)
├── capture.py      — Stop hook handler, session summary extractor
├── bootstrap.py    — Historical session importer
├── installer.py    — Claude Code setup (settings.json + CLAUDE.md)
└── cli.py          — CLI entry point
tests/
├── conftest.py     — Shared fixtures (tmp_db)
├── test_db.py      — DB layer tests
└── test_mcp.py     — MCP tool tests
```

---

## Key constraints

- No Docker, no external services — SQLite only
- No auto-extraction from session content (explicit save or continuation summaries only)
- Python 3.10+ compatibility required
- Never write to `~/.brainvault/memory.db` in tests — use the `tmp_db` fixture

---

## Submitting a pull request

1. Fork the repo and create a branch from `main`
2. Make your changes with tests
3. Ensure `pytest` and `ruff check .` both pass
4. Open a PR — describe what you changed and why

---

## Reporting bugs

Open a GitHub issue. Include:
- Python version (`python --version`)
- How brainvault was installed
- Steps to reproduce
- What you expected vs. what happened

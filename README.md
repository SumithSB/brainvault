# Brainvault

Personal memory layer for Claude Code. Stores what you've built, how you think, and decisions you've made — across every session.

SQLite + FTS5 + MCP. Zero infrastructure. One install command.

---

## The Problem

Every Claude Code session starts cold. You explain your stack, your preferences, your past decisions — over and over. Switch projects, start a new session, same thing again.

Brainvault fixes that. Save once, recall forever.

---

## Install

```bash
pip install brainvault
brainvault install
```

Then restart Claude Code. That's it.

Optionally seed your vault from existing conversation history:

```bash
brainvault bootstrap
```

---

## How It Works

Brainvault runs as a local MCP server backed by SQLite. Claude Code connects to it automatically after install.

**During a session**, Claude will:
- Call `get_my_context()` at the start to load your profile and active projects
- Save memories when you say *"remember this"*, *"save this"*, *"note this"*
- Auto-capture structured session summaries via a Stop hook after each session

**Across sessions**, memories are searchable by keyword, type, and project — so context carries forward without re-explaining.

---

## Commands

```bash
brainvault install      # Set up MCP server + Stop hook in Claude Code
brainvault bootstrap    # Seed memory from existing Claude Code session history
brainvault search <query> [--project <name>]   # Search memories from terminal
brainvault stats        # Show memory statistics
```

---

## Memory Types

| Type | When it's used |
|---|---|
| `profile` | Who you are, your preferences, your working style |
| `decision` | Architectural choices and the reasoning behind them |
| `pattern` | How you always approach something ("always use async endpoints") |
| `note` | Anything else worth remembering |

---

## Project Structure

```
brainvault/
├── db.py           — SQLite schema, CRUD, FTS5 full-text search
├── mcp_server.py   — 6 MCP tools served via stdio
├── capture.py      — Stop hook handler, session summary extractor
├── bootstrap.py    — Historical session importer
├── installer.py    — Patches ~/.claude/settings.json + CLAUDE.md
└── cli.py          — CLI entry point
```

**DB location:** `~/.brainvault/memory.db`

---

## MCP Tools

| Tool | What it does |
|---|---|
| `get_my_context` | Load your profile, active projects, and stats |
| `save_memory` | Save a memory (profile / decision / pattern / note) |
| `search_memory` | Full-text search across all memories |
| `register_project` | Register or update a project (name, stack, description) |
| `get_project` | Get everything stored about a specific project |
| `forget` | Delete a memory by ID |

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

**Requirements:** Python 3.10+, Claude Code

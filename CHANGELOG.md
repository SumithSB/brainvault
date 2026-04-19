# Changelog

All notable changes to brainvault will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.1.0] - 2026-04-19

### Added

- **MCP memory vault** — 12 tools (`get_my_context`, `save_memory`, `search_memory`, `register_project`, `get_project`, `record_outcome`, `reflect`, `update_memory`, `forget`, `get_code_context`, `get_recent_activity`, `get_session_timeline`) served via stdio. SQLite + FTS5 storage at `~/.brainvault/memory.db`.
- **Claude Code integration** — patches `~/.claude/settings.json` (MCP + Stop + PostToolUse hooks) and `~/.claude/CLAUDE.md`. Hooks auto-capture session notes, git history, repo index, and embeddings after every turn.
- **Cursor integration** — patches `~/.cursor/mcp.json`, `~/.cursor/rules/brainvault.mdc`, and `~/.cursor/hooks.json` (stop, postToolUse, afterFileEdit, afterShellExecution).
- **`brainvault save`** — quickly capture a memory from the terminal without opening Claude (`--type`, `--project`, stdin support).
- **`brainvault forget --project <name>`** — bulk-delete all memories for a project. MCP `forget` tool accepts optional `project` param.
- **`brainvault bootstrap`** — bulk-import past Claude Code + Cursor session transcripts.
- **`brainvault bootstrap-git`** — discover and scan all local git repos; mine commits for decision/pattern memories.
- **`brainvault git-scan`** — mine a single repo's git history.
- **`brainvault index-repo`** — index file structure and co-change matrix for a repo.
- **`brainvault graph`** — self-contained D3.js force-directed HTML brain graph; two-layer (memory circles + code file nodes); 6 edge types; layer/edge toggles and full-text search.
- **`brainvault doctor`** — diagnose install health (DB, MCP, hooks, optional deps).
- **`brainvault export` / `import`** — dump and restore memories as JSON or Markdown.
- **`brainvault reflect`** — surface open decisions, cross-project patterns, stale projects, outcome sentiment.
- **`brainvault status` / `stats` / `sessions` / `activity`** — vault health and session replay.
- **Optional semantic search** — `pip install 'brainvault[semantic]'` adds fastembed + sqlite-vec; search blends FTS5 + cosine similarity via Reciprocal Rank Fusion.
- **Auto-capture mining** — Stop hook extracts decision/pattern memories from session transcripts via scored regex rules (threshold 3, cap 20/session).
- **Windows hook support** — hook commands use forward-slash paths (`C:/Users/.../python.exe`) compatible with cmd.exe and PowerShell.
- Full test suite (377 tests) with isolated temp DB and mock embeddings. CI matrix: Ubuntu, macOS, Windows × Python 3.10/3.12.

# Changelog

All notable changes to brainvault will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.1.0] - 2026-04-09

### Added
- SQLite + FTS5 storage layer with full-text search (`db.py`)
- MCP server exposing 6 tools: `get_my_context`, `save_memory`, `search_memory`, `register_project`, `get_project`, `forget` (`mcp_server.py`)
- Stop hook handler that auto-captures Claude-generated continuation summaries after each session (`capture.py`)
- Bootstrap importer that seeds memory from all existing Claude Code session history (`bootstrap.py`)
- CLI with `install`, `bootstrap`, `search`, and `stats` commands (`cli.py`)
- One-command setup: patches `~/.claude/settings.json` and `~/.claude/CLAUDE.md` (`installer.py`)
- Full test suite (29 tests) covering DB layer and MCP tools
- `py.typed` marker for type checker support

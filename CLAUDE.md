# Brainvault

Personal memory layer for Claude Code. SQLite + FTS5 + MCP. Zero infrastructure.

## Key constraints
- No Docker, no external services, SQLite only
- Explicit save model + Stop hook auto-capture
- Python 3.10+, Claude Code only (Cursor deferred)

## Structure
- `brainvault/db.py` — storage layer, all SQLite operations
- `brainvault/mcp_server.py` — 6 MCP tools served via stdio
- `brainvault/capture.py` — Stop hook handler, JSONL continuation summary extractor
- `brainvault/installer.py` — patches ~/.claude/settings.json + CLAUDE.md

## Running
```bash
pip install -e .
brainvault install
python -m brainvault.mcp_server   # MCP server (stdio)
python -m brainvault.capture      # Stop hook handler
```

## Testing
```bash
pytest tests/ -v
```

## DB location
~/.brainvault/memory.db

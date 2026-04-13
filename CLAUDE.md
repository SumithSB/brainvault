# Brainvault

Personal memory layer for Claude Code. SQLite + FTS5 + optional semantic search + MCP. Zero infrastructure.

## Key constraints
- No Docker, no external services, SQLite only
- Python 3.10+, Claude Code only (Cursor deferred)
- Optional semantic extras: `pip install 'brainvault[semantic]'` (fastembed + sqlite-vec)

## Structure
- `brainvault/db.py` — storage layer: SQLite schema, FTS5, vector search, reflection queries
- `brainvault/mcp_server.py` — 10 MCP tools served via stdio
- `brainvault/cli.py` — CLI entry point (14 commands)
- `brainvault/capture.py` — Stop hook handler, JSONL continuation summary extractor
- `brainvault/installer.py` — patches ~/.claude/settings.json + ~/.claude/CLAUDE.md (upgrades in place); auto-seeds vault from git history + session summaries on first install
- `brainvault/bootstrap.py` — imports past Claude Code session history
- `brainvault/git_scan.py` — mines git history for architectural decision memories; discover_repos() for full-system scan
- `brainvault/code_scan.py` — file tree walker, regex import extractor, co-change matrix builder; index_repo() orchestrator
- `brainvault/embeddings.py` — fastembed wrapper (BAAI/bge-small-en-v1.5, lazy-loaded singleton)
- `brainvault/graph.py` — generates self-contained HTML brain graph (D3.js force-directed);
  two-layer visualization: memory circles + teal diamond code file nodes (from code_entities/
  code_cochange tables populated by index-repo); 6 edge types: belongs_to, file_overlap,
  temporal, keyword_overlap, cochange (files that change together), memory_file (commit→file);
  Layer filter chips, edge toggles, full-text search across paths/languages/keywords

## Running
```bash
pip install -e .
brainvault install
python -m brainvault.mcp_server   # MCP server (stdio)
python -m brainvault.capture      # Stop hook handler
```

## CLI commands
```
install         Set up MCP server + Stop hook + CLAUDE.md; auto-seeds vault from git history + past sessions
init            Onboard a new project interactively
bootstrap       Seed from existing Claude Code session history
bootstrap-git   Discover and scan all local git repos (default: ~)
git-scan        Scan a single git repo
index-repo      Index file structure and co-change matrix for a repo
search          Search memories from terminal
status          Vault health at a glance
update          Edit a memory by ID
reflect         Open decisions, cross-project patterns, outcome sentiment
forget          Delete a memory by ID
embed           Backfill semantic embeddings
graph           Generate HTML brain graph (--open to launch browser)
stats           Memory counts by type/project
```

## MCP tools (10)
`get_my_context` · `save_memory` · `search_memory` · `register_project` · `get_project` · `record_outcome` · `reflect` · `update_memory` · `forget` · `get_code_context`

## Testing
```bash
pytest tests/ -v
```

## DB location
`~/.brainvault/memory.db`

## Key db.py functions
- `save_memory` / `search_memories` / `update_memory` / `delete_memory`
- `record_outcome(memory_id, outcome, sentiment)` — sentiment: positive/negative/mixed
- `get_reflection_data()` — open decisions, stale projects, cross-project patterns, outcome_sentiment_summary
- `get_status()` — vault health aggregate
- `is_commit_scanned` / `mark_commit_scanned` — deduplication for git-scan
- `get_unembedded_memories` / `store_embedding` / `count_embedded`
- `index_repo_files` / `bulk_record_cochange` / `update_code_index_run` — code intelligence writes
- `is_repo_indexed` / `get_project_repo_path` / `get_code_context_data` — code intelligence reads
- `VALID_MEMORY_TYPES` — frozenset, single source of truth for type validation

## Schema migrations
All additive — handled in `_migrate(conn)` called from `init_db()`. Never drop columns.

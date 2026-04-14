# Brainvault

Personal memory layer for Claude Code. SQLite + FTS5 + optional semantic search + MCP. Zero infrastructure.

## Key constraints
- No Docker, no external services, SQLite only
- Python 3.10+, Claude Code only (Cursor deferred)
- Optional semantic extras: `pip install 'brainvault[semantic]'` (fastembed + sqlite-vec)

## Structure
- `brainvault/db.py` — storage layer: SQLite schema, FTS5, vector search, reflection queries
- `brainvault/mcp_server.py` — 12 MCP tools served via stdio
- `brainvault/cli.py` — CLI entry point (16 commands)
- `brainvault/capture.py` — Stop hook handler, JSONL continuation summary extractor
- `brainvault/tool_capture.py` — PostToolUse hook handler; records Write/Edit/Bash/TodoWrite/NotebookEdit events as session replay buffer; <20 ms per event, never crashes
- `brainvault/installer.py` — patches ~/.claude/settings.json + ~/.claude/CLAUDE.md (upgrades in place); registers Stop + PostToolUse hooks; auto-seeds vault from git history + session summaries on first install
- `brainvault/bootstrap.py` — imports past Claude Code session history
- `brainvault/git_scan.py` — mines git history for architectural decision memories; discover_repos() for full-system scan
- `brainvault/code_scan.py` — file tree walker, regex import extractor, co-change matrix builder; index_repo() orchestrator
- `brainvault/embeddings.py` — fastembed wrapper (BAAI/bge-small-en-v1.5, lazy-loaded singleton)
- `brainvault/graph.py` — generates self-contained HTML brain graph (D3.js force-directed);
  two-layer visualization: memory circles + teal diamond code file nodes (from code_entities/
  code_cochange tables populated by index-repo); 6 edge types: belongs_to, file_overlap,
  temporal, keyword_overlap, cochange (files that change together), memory_file (commit→file);
  Layer filter chips, edge toggles, full-text search across paths/languages/keywords

## Install scope
One install covers all projects. Patches `~/.claude/settings.json` + `~/.claude/CLAUDE.md` — global Claude Code config, not project-specific. DB at `~/.brainvault/memory.db` is shared across all projects.

## Running
```bash
pip install -e .
brainvault install
python -m brainvault.mcp_server   # MCP server (stdio)
python -m brainvault.capture      # Stop hook handler (runs maintenance pass: summaries, git scan, index, embed)
python -m brainvault.tool_capture # PostToolUse hook handler (reads JSON from stdin)
```

## CLI commands
```
install         Set up MCP server + Stop + PostToolUse hooks + CLAUDE.md; auto-seeds vault
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
sessions        List recent Claude Code sessions and their activity
activity        Show full event timeline for a specific session ID
```

## MCP tools (12)
`get_my_context` · `save_memory` · `search_memory` · `register_project` · `get_project` · `record_outcome` · `reflect` · `update_memory` · `forget` · `get_code_context` · `get_recent_activity` · `get_session_timeline`

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
- `get_stats()` — vault health aggregate (total_memories, by_type, by_source, unembedded, git stats, last session, open decisions, stale projects)
- `is_commit_scanned` / `mark_commit_scanned` — deduplication for git-scan
- `get_unembedded_memories` / `store_embedding` / `count_embedded`
- `index_repo_files` / `bulk_record_cochange` / `update_code_index_run` — code intelligence writes
- `is_repo_indexed` / `get_project_repo_path` / `get_code_context_data` — code intelligence reads
- `record_tool_event` / `get_session_timeline` / `get_recent_activity(project, days, limit_sessions)` / `prune_old_events(days=90)` — session replay buffer; events pruned after 90 days
- `is_session_captured` / `mark_session_captured` — deduplication for Stop hook capture
- `VALID_MEMORY_TYPES` — frozenset: `{'profile', 'project', 'decision', 'pattern', 'note'}`

## Hook details
- **Stop hook** matcher: `""` (fires on every stop) → `python -m brainvault.capture`
  Runs 4-step maintenance pass: session summaries → git scan → repo index (first-time + daily refresh, skip if >5k files) → embedding backfill (up to 20/turn)
- **PostToolUse hook** matcher: `"Write|Edit|Bash|TodoWrite|NotebookEdit"` → `python -m brainvault.tool_capture`
  Records tool events into session_events replay buffer; <20 ms, never crashes
- Both hooks quote `sys.executable` to handle paths with spaces

## Schema migrations
All additive — handled in `_migrate(conn)` called from `init_db()`. Never drop columns.
Tables: `memories`, `projects`, `sessions_captured`, `memory_links`, `memories_fts` (virtual), `memory_vectors`, `git_commits_scanned`, `code_entities`, `code_cochange`, `code_index_runs`, `session_events`

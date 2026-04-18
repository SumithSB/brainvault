"""
brainvault/capture.py — Stop hook handler (Claude Code, Cursor).

Called after each agent turn: ``python -m brainvault.capture``

Delegates session JSONL parsing to each concrete ``AgentAdapter``; keeps
agent-neutral maintenance (replay prune, git scan, code index, embeddings) here.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from brainvault import db
from brainvault.adapters import ALL_ADAPTERS


def process_session(session_path: Path, adapter) -> int:
    """
    Process a single session file. Extract continuation summaries and save to DB.
    Returns number of memories saved.
    """
    db.init_db()

    if db.is_session_captured(str(session_path)):
        return 0

    project = adapter.extract_project_name(session_path)
    chunks = adapter.parse_session_file(session_path)

    saved = 0
    for chunk in chunks:
        db.save_memory(
            content=chunk,
            memory_type="note",
            project=project,
            source="hook",
            source_agent=adapter.name,
        )
        saved += 1

    db.mark_session_captured(str(session_path), memory_count=saved, source_agent=adapter.name)
    return saved


def _maybe_run_git_scan() -> int:
    """
    Check cwd for new commits since last scan. Run scan_repo if any found.

    Fast path: get the latest commit hash (one git call), check if already
    scanned (one DB lookup). Only iterates commits when something is new.
    Returns number of memories saved.
    """
    try:
        from brainvault.git_scan import _resolve_repo_path, _run_git, scan_repo

        repo_path = _resolve_repo_path(Path.cwd())
        repo_key = str(repo_path)

        # Check if the latest commit is already scanned — common case, very fast
        latest_hash = _run_git(["log", "-1", "--format=%H"], cwd=repo_path).strip()
        if not latest_hash:
            return 0  # no commits yet

        if db.is_commit_scanned(repo_key, latest_hash):
            return 0  # up to date

        # New commits exist — find since date from last scanned commit
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(scanned_at) FROM git_commits_scanned WHERE repo_path = ?",
                (repo_key,),
            ).fetchone()
        since_date = (
            row[0][:10]
            if row and row[0]
            else (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
        )

        project = repo_path.name
        stats = scan_repo(repo_path, project=project, since=since_date, limit=100, verbose=False)
        return stats["commits_saved"]
    except Exception as e:
        print(f"[brainvault] git scan skipped: {e}", file=sys.stderr)
        return 0


_AUTO_INDEX_FILE_LIMIT = 5000  # skip auto-index on repos larger than this


def _maybe_reindex_repo() -> bool:
    """
    Index or re-index the cwd repo silently.

    First-time logic: if repo has never been indexed, count source files first.
    Skip if >5 000 files (too large to index unexpectedly in the background).
    Otherwise index it now.

    Refresh logic: if already indexed, re-index only when >24 h stale.

    Returns True if indexing ran.
    """
    try:
        import datetime

        from brainvault.code_scan import index_repo, scan_file_tree
        from brainvault.git_scan import _resolve_repo_path

        repo_path = _resolve_repo_path(Path.cwd())
        repo_key = str(repo_path)
        project = repo_path.name

        if not db.is_repo_indexed(repo_key):
            # First time — count files before committing to a full index
            files, _ = scan_file_tree(repo_path)
            if len(files) > _AUTO_INDEX_FILE_LIMIT:
                return False  # too large; let the user run index-repo manually
            index_repo(repo_path, project=project, verbose=False)
            return True

        # Already indexed — refresh only if stale
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT indexed_at FROM code_index_runs WHERE repo_path = ?",
                (repo_key,),
            ).fetchone()

        if not row:
            return False

        last_indexed = datetime.datetime.fromisoformat(row[0])
        age_hours = (datetime.datetime.now() - last_indexed).total_seconds() / 3600
        if age_hours < 24:
            return False  # fresh enough

        index_repo(repo_path, project=project, verbose=False)
        return True
    except Exception as e:
        print(f"[brainvault] reindex skipped: {e}", file=sys.stderr)
        return False


def _maybe_backfill_embeddings() -> int:
    """
    Silently backfill up to 20 unembedded memories if fastembed is available.
    Caps at 20 per hook invocation to avoid blocking the hook for too long.
    Returns number of memories embedded.
    """
    try:
        from brainvault import embeddings as emb

        if not emb._is_available():
            return 0

        unembedded = db.get_unembedded_memories()
        if not unembedded:
            return 0

        count = 0
        for mem in unembedded[:20]:
            vector = emb.embed(mem["content"])
            db.store_embedding(mem["id"], vector)
            count += 1
        return count
    except Exception as e:
        print(f"[brainvault] embedding backfill skipped: {e}", file=sys.stderr)
        return 0


def run() -> None:
    """
    Entry point called by the Stop hook (Claude Code or Cursor).
    0. Prune stale session replay rows (session_events older than 90 days).
    1. Capture session notes from recent transcripts per adapter.
    2. Scan cwd git repo for new commits (skips already-scanned ones).
    3. Re-index cwd repo file structure if stale (>24 h since last index).
    4. Backfill semantic embeddings for any unembedded memories.
    """
    db.init_db()

    try:
        deleted = db.prune_old_events(days=90)
        if deleted > 0:
            print(f"[brainvault] Pruned {deleted} old session_events rows.", file=sys.stderr)
    except Exception as e:
        print(f"[brainvault] session_events prune skipped: {e}", file=sys.stderr)

    # 1. Session summaries (each adapter owns transcript layout + parsing)
    total_saved = 0
    for cls in ALL_ADAPTERS:
        adapter = cls()
        recent = adapter.recent_session_files(max_age_seconds=300)
        for session_path in recent:
            try:
                saved = process_session(session_path, adapter)
                total_saved += saved
            except Exception as e:
                print(
                    f"[brainvault] session capture skipped ({session_path}): {e}",
                    file=sys.stderr,
                )

    if total_saved > 0:
        print(f"[brainvault] Captured {total_saved} memories from session.", file=sys.stderr)

    # 2. Git scan — pick up new commits in cwd repo
    git_saved = _maybe_run_git_scan()
    if git_saved > 0:
        print(f"[brainvault] Git scan: {git_saved} new memories.", file=sys.stderr)

    # 3. Re-index repo structure if stale
    if _maybe_reindex_repo():
        print("[brainvault] Re-indexed repo file structure.", file=sys.stderr)

    # 4. Backfill embeddings
    embedded = _maybe_backfill_embeddings()
    if embedded > 0:
        print(f"[brainvault] Backfilled {embedded} embeddings.", file=sys.stderr)


if __name__ == "__main__":
    run()

"""
brainvault/capture.py — Stop hook handler and JSONL continuation summary extractor.

Called by Claude Code's Stop hook after each agent turn:
    python -m brainvault.capture

Scans the most recently modified JSONL session file in ~/.claude/projects/
Looks specifically for Claude-generated continuation summaries — these are
high-quality structured summaries already written by Claude when context runs out.
No rule-based extraction. Only Claude-generated summaries get saved.
"""

import json
import re
import sys
import time
from pathlib import Path

from brainvault import db

CONTINUATION_MARKER = "This session is being continued from a previous conversation"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def get_recent_session_files(max_age_seconds: int = 300) -> list[Path]:
    """Return JSONL files modified within the last N seconds, newest first."""
    now = time.time()
    candidates = []
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    for jsonl in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            mtime = jsonl.stat().st_mtime
            if now - mtime <= max_age_seconds:
                candidates.append((mtime, jsonl))
        except OSError:
            continue
    candidates.sort(reverse=True)
    return [p for _, p in candidates]


def extract_project_name(session_path: Path) -> str:
    """
    Derive project name from the encoded directory name.

    The directory encodes the full path with '-' replacing '/':
        -Users-sumithsb-Projects-job-tracking-assistant
         ^     ^        ^        ^--- project name (everything after parent dir)
         |     |        +-- parent category (Projects, Tml, UoL, ...)
         |     +-- username
         +-- 'Users'

    Strategy: skip the first 4 segments (leading empty, 'Users', username,
    parent category) and rejoin the remainder — this preserves multi-word names.

    Examples:
        -Users-sumithsb-Projects-job-tracking-assistant → job-tracking-assistant
        -Users-sumithsb-Tml-pluto-api                  → pluto-api
        -Users-sumithsb-Projects-InterviewAI            → InterviewAI
    """
    dir_name = session_path.parent.name.rstrip("-")
    parts = dir_name.split("-")
    # parts[0] = '' (leading '-'), parts[1] = 'Users', parts[2] = username,
    # parts[3] = parent category — skip all four
    if len(parts) < 5:
        # Unexpected path structure — use directory name as fallback
        return dir_name.lstrip("-") or "unknown"
    name_parts = parts[4:]
    return "-".join(name_parts)


def chunk_summary(summary: str) -> list[str]:
    """
    Split a continuation summary into focused chunks by markdown section (## heading).
    Each chunk is one section, which makes search results specific and readable.
    Falls back to the full summary if no sections are found.
    """
    # Split on ## headings, keeping the heading with its content
    sections = re.split(r"(?=^##\s)", summary, flags=re.MULTILINE)
    chunks = [s.strip() for s in sections if s.strip() and len(s.strip()) > 80]
    return chunks if chunks else [summary]


def extract_continuation_summaries(session_path: Path) -> list[str]:
    """
    Parse a JSONL session file and extract Claude-generated continuation summaries.
    These are injected as user messages at the start of continuation sessions.
    Pattern: content starts with CONTINUATION_MARKER.
    Returns list of summary text strings.
    """
    summaries = []
    try:
        with open(session_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event.get("type") != "user":
                    continue

                content = event.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )

                if isinstance(content, str) and CONTINUATION_MARKER in content:
                    # Extract just the summary portion after the marker header
                    summary = clean_continuation_summary(content)
                    if summary:
                        summaries.append(summary)
    except OSError:
        pass
    return summaries


def clean_continuation_summary(raw: str) -> str:
    """
    Strip the boilerplate header from a continuation summary, returning clean text.
    The summaries start with 'This session is being continued...' followed by
    the actual structured summary content.
    """
    # Strip IDE injection noise before the marker
    if CONTINUATION_MARKER in raw:
        idx = raw.index(CONTINUATION_MARKER)
        raw = raw[idx:]

    # Remove the first line (the boilerplate sentence)
    lines = raw.split("\n")
    content_lines = []
    skip_next = False
    for i, line in enumerate(lines):
        if i == 0:
            continue  # skip "This session is being continued..."
        if line.strip().lower().startswith("summary"):
            skip_next = True
            continue
        if skip_next and not line.strip():
            skip_next = False
            continue
        content_lines.append(line)

    result = "\n".join(content_lines).strip()
    return result if len(result) > 100 else ""  # skip trivially short summaries


def process_session(session_path: Path) -> int:
    """
    Process a single session file. Extract continuation summaries and save to DB.
    Returns number of memories saved.
    """
    db.init_db()

    if db.is_session_captured(str(session_path)):
        return 0

    project = extract_project_name(session_path)
    summaries = extract_continuation_summaries(session_path)

    saved = 0
    for summary in summaries:
        for chunk in chunk_summary(summary):
            db.save_memory(
                content=chunk,
                memory_type="note",
                project=project,
                source="hook",
            )
            saved += 1

    db.mark_session_captured(str(session_path), memory_count=saved)
    return saved


def _maybe_run_git_scan() -> int:
    """
    Check cwd for new commits since last scan. Run scan_repo if any found.

    Fast path: get the latest commit hash (one git call), check if already
    scanned (one DB lookup). Only iterates commits when something is new.
    Returns number of memories saved.
    """
    try:
        import datetime

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
    Entry point called by the Claude Code Stop hook.
    1. Capture continuation summaries from recent sessions.
    2. Scan cwd git repo for new commits (skips already-scanned ones).
    3. Re-index cwd repo file structure if stale (>24 h since last index).
    4. Backfill semantic embeddings for any unembedded memories.
    """
    db.init_db()

    # 1. Session summaries
    recent = get_recent_session_files(max_age_seconds=300)
    total_saved = 0
    for session_path in recent:
        try:
            saved = process_session(session_path)
            total_saved += saved
        except Exception as e:
            print(f"[brainvault] session capture skipped ({session_path}): {e}", file=sys.stderr)

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

"""
brainvault/capture.py — Stop hook handler for supported coding-agent hosts.

Called after each agent turn: ``python -m brainvault.capture``

Delegates session JSONL parsing to each concrete ``AgentAdapter``; keeps
agent-neutral maintenance (replay prune, git scan, code index, embeddings) here.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

from brainvault import db
from brainvault.adapters import ALL_ADAPTERS
from brainvault.adapters._redact import redact_sensitive

# --- transcript mining (assistant paragraphs → decision / pattern memories) ---
# Conservative defaults: higher bar, fewer auto-saves.

_MIN_MINED_CHARS = 120
_MAX_MINED_CHARS = 6000
_MAX_MINED_PER_SESSION = 20
_MINED_SCORE_THRESHOLD = 3
_MINED_HIGH_CONFIDENCE_SCORE = 7
_PREFIX_DEDUPE_CHARS = 200

# Recapture when transcript grows meaningfully (append-only JSONL).
_RECAPTURE_MIN_LINE_DELTA = 40
_RECAPTURE_MIN_SIZE_RATIO = 0.12
_RECAPTURE_MIN_INTERVAL_SEC = 30

# (regex, points, "decision" | "pattern")
_MINING_RULES: tuple[tuple[re.Pattern[str], int, str], ...] = (
    # Patterns — debugging / corrections
    (re.compile(r"(?i)\broot cause\b"), 5, "pattern"),
    (re.compile(r"(?i)\bthe (?:fix|bug|issue|problem)\s+(?:was|is)\b"), 4, "pattern"),
    (re.compile(r"(?i)\b(?:workaround|hotfix|patch)\b"), 3, "pattern"),
    (re.compile(r"(?i)\b(?:regression|false positive)\b"), 3, "pattern"),
    (re.compile(r"(?i)\b(?:resolved|fixed)\s+by\b"), 3, "pattern"),
    (re.compile(r"(?i)\bcaused\s+by\b"), 3, "pattern"),
    (re.compile(r"(?i)\buser\s+(?:corrected|clarified|pointed out)\b"), 4, "pattern"),
    (re.compile(r"(?i)\b(?:should not|must not|avoid)\s+\w+"), 2, "pattern"),
    # Additional pattern signals
    (re.compile(r"(?i)\b(?:discovered|realized|noticed)\s+that\b"), 2, "pattern"),
    (re.compile(r"(?i)\bthe (?:real|actual|underlying)\s+(?:issue|problem|cause)\b"), 4, "pattern"),
    (re.compile(r"(?i)\b(?:never|always)\s+(?:use|call|pass|set|return)\b"), 3, "pattern"),
    (re.compile(r"(?i)\b(?:exception|traceback|stack\s*trace)\b"), 2, "pattern"),
    # Decisions — tradeoffs and intent
    (re.compile(r"(?i)\btrade-?offs?\b"), 4, "decision"),
    (re.compile(r"(?i)\bwe (?:chose|picked|selected|went with|decided on)\b"), 4, "decision"),
    (re.compile(r"(?i)\b(?:design|architecture)\s+(?:choice|decision|rationale)\b"), 4, "decision"),
    (re.compile(r"(?i)\brather than\b.+\b(?:we|I)\s+(?:use|chose|pick|went)", re.DOTALL), 4, "decision"),
    (re.compile(r"(?i)\b(?:instead of|in favor of)\b"), 2, "decision"),
    (re.compile(r"(?i)\b(?:long-?term|scalability|maintainability)\b"), 2, "decision"),
    (re.compile(r"(?i)\bbecause\b.+\b(?:performance|security|latency|cost|team)\b", re.DOTALL), 3, "decision"),
    # Additional decision signals
    (re.compile(r"(?i)\b(?:opted|opting)\s+(?:for|to)\b"), 3, "decision"),
    (re.compile(r"(?i)\bthe\s+(?:reason|rationale)\s+(?:is|was|for)\b"), 4, "decision"),
    (re.compile(r"(?i)\b(?:pros\s+and\s+cons|trade.?off)\b"), 3, "decision"),
    (re.compile(r"(?i)\bgoing\s+with\b"), 2, "decision"),
)

_GENERIC_OPENERS = re.compile(
    r"(?is)^\s*(?:thanks|thank you|great question|sure[!,.]?|okay[,!.]?|"
    r"let me know if|happy to help|here('s| is) a|i('ll| will) (?:start|begin|help))\b"
)


def _normalize_for_dedupe(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _strip_fenced_code(text: str) -> str:
    return re.sub(r"```[\s\S]*?```", " ", text)


def _assistant_text_from_claude_event(event: dict) -> str:
    if event.get("type") != "assistant":
        return ""
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        t = block.get("text", "")
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


def _assistant_text_from_cursor_event(event: dict) -> str:
    if event.get("role") != "assistant":
        return ""
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        t = block.get("text", "")
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


def _iter_assistant_message_texts(path: Path, source_agent: str) -> list[str]:
    reader = _assistant_text_from_claude_event if source_agent == "claude_code" else _assistant_text_from_cursor_event
    out: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = reader(event)
                if text.strip():
                    out.append(text)
    except OSError:
        return []
    return out


def _split_into_paragraphs(blob: str) -> list[str]:
    """Split assistant text into paragraph-ish units for scoring."""
    chunks = re.split(r"\n{2,}", blob.strip())
    paras: list[str] = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        if len(c) > 3200:
            for line_block in re.split(r"(?<=\n)(?=#+\s)", c):
                line_block = line_block.strip()
                if len(line_block) >= _MIN_MINED_CHARS:
                    paras.append(line_block)
        elif len(c) >= _MIN_MINED_CHARS:
            paras.append(c)
    return paras


def _score_paragraph(paragraph: str) -> tuple[str | None, int]:
    """Return (memory_type or None, winning_score)."""
    probe = _strip_fenced_code(paragraph)
    if len(probe.strip()) < _MIN_MINED_CHARS:
        return None, 0
    d_score = 0
    p_score = 0
    for _rx, pts, kind in _MINING_RULES:
        if _rx.search(probe):
            if kind == "decision":
                d_score += pts
            else:
                p_score += pts
    if d_score < _MINED_SCORE_THRESHOLD and p_score < _MINED_SCORE_THRESHOLD:
        return None, 0
    if d_score > p_score:
        return "decision", d_score
    if p_score > d_score:
        return "pattern", p_score
    # tie — prefer pattern when debugging language dominates
    if re.search(r"(?i)\b(?:fix|bug|error|fail|cause)\b", probe):
        return "pattern", p_score
    return "decision", d_score


def _paragraph_too_generic(paragraph: str, score: int) -> bool:
    head = paragraph[:220]
    if score >= _MINED_HIGH_CONFIDENCE_SCORE + 1:
        return False
    return bool(_GENERIC_OPENERS.search(head))


def mine_session_transcript(session_path: Path, source_agent: str) -> list[tuple[str, str, int]]:
    """
    Scan JSONL for assistant messages; extract high-signal paragraphs.

    Returns list of (content, memory_type, score) with memory_type in {decision, pattern}.
    """
    if source_agent not in ("claude_code", "cursor"):
        return []

    scored: list[tuple[int, str, str]] = []
    seen_prefix: set[str] = set()

    for blob in _iter_assistant_message_texts(session_path, source_agent):
        for para in _split_into_paragraphs(blob):
            para = para.strip()
            if len(para) > _MAX_MINED_CHARS:
                para = para[: _MAX_MINED_CHARS - 3].rstrip() + "..."
            mtype, pts = _score_paragraph(para)
            if mtype is None or pts < _MINED_SCORE_THRESHOLD:
                continue
            if _paragraph_too_generic(para, pts):
                continue
            n = _normalize_for_dedupe(para)
            if len(n) < 40:
                continue
            prefix = n[:_PREFIX_DEDUPE_CHARS]
            if prefix in seen_prefix:
                continue
            seen_prefix.add(prefix)
            scored.append((pts, mtype, para))

    scored.sort(key=lambda x: -x[0])
    picked: list[tuple[str, str, int]] = []
    bodies_norm: list[str] = []

    for pts, mtype, para in scored[: _MAX_MINED_PER_SESSION * 2]:
        n = _normalize_for_dedupe(para)
        if any(n in b or b in n for b in bodies_norm if len(b) > 80 and len(n) > 80):
            continue
        bodies_norm.append(n)
        safe = redact_sensitive(para, max_len=min(len(para), _MAX_MINED_CHARS))
        picked.append((safe, mtype, pts))
        if len(picked) >= _MAX_MINED_PER_SESSION:
            break

    return picked


def _dedupe_mined_against_chunks(
    mined: list[tuple[str, str, int]], chunks: list[str]
) -> list[tuple[str, str, int]]:
    chunk_norms = [_normalize_for_dedupe(c) for c in chunks if len(c) > 60]
    out: list[tuple[str, str, int]] = []
    for content, mtype, pts in mined:
        n = _normalize_for_dedupe(content)
        if any(n in cn or cn in n for cn in chunk_norms if len(cn) > 80 and len(n) > 80):
            continue
        out.append((content, mtype, pts))
    return out


def _session_file_metrics(path: Path) -> tuple[int, int] | None:
    """Return (byte_length, line_count) for transcript file."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        return (len(raw), line_count)
    except OSError:
        return None


def _should_recapture_session(
    row: object,
    new_bytes: int,
    new_lines: int,
) -> bool:
    """True if transcript grew enough since last capture (conservative)."""
    old_b = row["transcript_bytes"]
    old_l = row["transcript_lines"]
    if old_b is None or old_l is None:
        return False
    if new_bytes <= old_b and new_lines <= old_l:
        return False
    line_growth = new_lines - int(old_l)
    size_ratio = (new_bytes - int(old_b)) / max(int(old_b), 1)
    if line_growth < _RECAPTURE_MIN_LINE_DELTA and size_ratio < _RECAPTURE_MIN_SIZE_RATIO:
        return False
    cap_at = row["captured_at"]
    if cap_at:
        try:
            last = datetime.datetime.fromisoformat(cap_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            age = datetime.datetime.now(datetime.timezone.utc) - last
            if age.total_seconds() < _RECAPTURE_MIN_INTERVAL_SEC:
                return False
        except (TypeError, ValueError):
            pass
    return True


def process_session(session_path: Path, adapter) -> int:
    """
    Process a single session file. Extract continuation summaries and save to DB.
    Returns number of memories saved.
    """
    db.init_db()

    sp = str(session_path)
    metrics = _session_file_metrics(session_path)
    if metrics is None:
        return 0
    new_bytes, new_lines = metrics

    row = db.get_session_capture_row(sp)
    if row is not None and row["transcript_bytes"] is None:
        db.seed_session_transcript_stats(sp, new_bytes, new_lines)
        return 0

    if row is not None:
        if not _should_recapture_session(row, new_bytes, new_lines):
            return 0

    project = adapter.extract_project_name(session_path)
    chunks = adapter.parse_session_file(session_path)
    mined = _dedupe_mined_against_chunks(
        mine_session_transcript(session_path, adapter.name),
        chunks,
    )

    saved = 0
    for chunk in chunks:
        if db.is_hook_capture_duplicate(chunk, project, source="hook", source_agent=adapter.name):
            continue
        db.save_memory(
            content=chunk,
            memory_type="note",
            project=project,
            source="hook",
            source_agent=adapter.name,
        )
        saved += 1

    for content, memory_type, score in mined:
        if db.is_hook_capture_duplicate(content, project, source="hook", source_agent=adapter.name):
            continue
        tier = (
            "high"
            if score >= _MINED_HIGH_CONFIDENCE_SCORE
            else "medium"
        )
        db.save_memory(
            content=content,
            memory_type=memory_type,
            project=project,
            keywords=["source_subtype:transcript_mined", f"mine_confidence:{tier}"],
            source="hook",
            source_agent=adapter.name,
        )
        saved += 1

    db.mark_session_captured(
        sp,
        memory_count=saved,
        source_agent=adapter.name,
        transcript_bytes=new_bytes,
        transcript_lines=new_lines,
    )
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
        if row and row[0]:
            since = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=datetime.timezone.utc)
        else:
            since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)

        project = repo_path.name
        stats = scan_repo(repo_path, project=project, since=since, limit=100, verbose=False)
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
    Entry point called by the Stop hook on each configured host.
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

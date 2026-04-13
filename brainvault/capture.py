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


def run() -> None:
    """
    Entry point called by the Claude Code Stop hook.
    Processes recently modified session files.
    """
    recent = get_recent_session_files(max_age_seconds=300)
    total_saved = 0
    for session_path in recent:
        try:
            saved = process_session(session_path)
            total_saved += saved
        except Exception:
            # Never crash the Stop hook — Claude Code must not be interrupted
            pass

    if total_saved > 0:
        print(f"[brainvault] Captured {total_saved} memories from session.", file=sys.stderr)


if __name__ == "__main__":
    run()

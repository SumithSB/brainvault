"""
brainvault/bootstrap.py — One-time import of historical Claude Code session data.

Scans all ~/.claude/projects/**/*.jsonl files and extracts:
1. Continuation summaries (Claude-generated structured summaries, high quality)
2. AI titles (session topic, saved for sessions with no continuation summary)

Run:
    brainvault bootstrap
    python -m brainvault.bootstrap
"""

import json
import sys
from pathlib import Path

from brainvault import db
from brainvault.adapters.claude_code import (
    CONTINUATION_MARKER,
    ClaudeCodeAdapter,
    chunk_summary,
    clean_continuation_summary,
    extract_project_name,
)

_CLAUDE_AGENT = ClaudeCodeAdapter.name

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _extract_session_data(session_path: Path) -> tuple[str | None, list[str]]:
    """
    Parse a JSONL session file and return:
      - ai_title: the session's AI-generated title (or None)
      - continuation_summaries: list of Claude-generated continuation summaries

    ai_title entries look like:
        {"type": "ai-title", "aiTitle": "Fix auth middleware bug"}

    Continuation summaries are user messages whose content starts with
    CONTINUATION_MARKER — they are rich structured summaries Claude already wrote.
    """
    ai_title = None
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

                event_type = event.get("type")

                if event_type == "ai-title":
                    title = event.get("aiTitle", "").strip()
                    if title:
                        ai_title = title

                elif event_type == "user":
                    content = event.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        )
                    if isinstance(content, str) and CONTINUATION_MARKER in content:
                        summary = clean_continuation_summary(content)
                        if summary:
                            summaries.append(summary)
    except OSError:
        pass

    return ai_title, summaries


def _get_all_session_files() -> list[Path]:
    """
    Return all top-level JSONL session files in ~/.claude/projects/, oldest first.
    Skips subagent files (nested under session subdirectories).
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return []

    files = []
    for jsonl in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            files.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue

    files.sort()  # oldest first so memories are stored in chronological order
    return [p for _, p in files]


def bootstrap(verbose: bool = True) -> dict:
    """
    Import all historical session data into brainvault.
    Idempotent — already-captured sessions are skipped via sessions_captured table.

    Returns a stats dict.
    """
    db.init_db()

    session_files = _get_all_session_files()

    stats = {
        "sessions_scanned": 0,
        "sessions_skipped": 0,
        "continuation_summaries": 0,
        "ai_titles": 0,
        "total_memories": 0,
    }

    for session_path in session_files:
        stats["sessions_scanned"] += 1

        if db.is_session_captured(str(session_path)):
            stats["sessions_skipped"] += 1
            continue

        project = extract_project_name(session_path)
        ai_title, summaries = _extract_session_data(session_path)

        saved = 0

        # Continuation summaries are highest quality — chunk by section and save each.
        for summary in summaries:
            for chunk in chunk_summary(summary):
                db.save_memory(
                    content=chunk,
                    memory_type="note",
                    project=project,
                    source="bootstrap",
                    source_agent=_CLAUDE_AGENT,
                )
                saved += 1
                stats["continuation_summaries"] += 1

        # AI title: save only when there's no continuation summary.
        # Continuation summaries already describe the session in far more detail,
        # so the title would be redundant noise if both exist.
        if not summaries and ai_title:
            db.save_memory(
                content=f"Session: {ai_title}",
                memory_type="note",
                project=project,
                source="bootstrap",
                source_agent=_CLAUDE_AGENT,
            )
            saved += 1
            stats["ai_titles"] += 1

        db.mark_session_captured(str(session_path), memory_count=saved, source_agent=_CLAUDE_AGENT)
        stats["total_memories"] += saved

        if verbose and saved > 0:
            label = summaries[0][:60] + "…" if summaries else ai_title
            print(f"  [{project}] {label}")

    return stats


def run() -> None:
    """CLI entry point: brainvault bootstrap"""
    print("Bootstrapping brainvault from Claude Code history…\n")

    if not CLAUDE_PROJECTS_DIR.exists():
        print(f"  ✗ No Claude Code projects found at {CLAUDE_PROJECTS_DIR}")
        sys.exit(1)

    stats = bootstrap(verbose=True)

    new_sessions = stats["sessions_scanned"] - stats["sessions_skipped"]

    print("\n  Done.")
    print(f"  · {stats['sessions_scanned']} sessions scanned")
    if stats["sessions_skipped"]:
        print(f"  · {stats['sessions_skipped']} already captured (skipped)")
    print(f"  · {new_sessions} new sessions processed")
    print(f"  · {stats['continuation_summaries']} continuation summaries saved")
    print(f"  · {stats['ai_titles']} AI titles saved")
    print(f"  · {stats['total_memories']} total memories added\n")


if __name__ == "__main__":
    run()

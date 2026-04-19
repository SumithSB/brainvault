"""
brainvault/bootstrap.py — Bulk-import historical session JSONL into the vault.

Imports from every configured host:

- **Claude Code** — ``~/.claude/projects/*/*.jsonl`` (continuation summaries + AI titles)
- **Cursor** — ``~/.cursor/projects/*/agent-transcripts/*/*.jsonl`` (same pipeline as the Stop hook)

The database and MCP tools are host-agnostic; this module only reads on-disk transcripts.

Run:
    brainvault bootstrap [--host claude_code|cursor|all]
    python -m brainvault.bootstrap
"""

from __future__ import annotations

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
from brainvault.adapters.cursor import CursorAdapter, iter_cursor_transcript_paths_under

_CLAUDE_AGENT = ClaudeCodeAdapter.name


def claude_projects_dir() -> Path:
    """~/.claude/projects — same as :attr:`ClaudeCodeAdapter.SESSIONS_PATH`, resolved at call time."""
    return ClaudeCodeAdapter.SESSIONS_PATH


def cursor_projects_dir() -> Path | None:
    """Cursor transcript root (``~/.cursor/projects``) if that directory exists."""
    return CursorAdapter().session_dir()


def _extract_session_data(session_path: Path) -> tuple[str | None, list[str]]:
    """
    Parse a JSONL session file and return:
      - ai_title: the session's AI-generated title (or None)
      - continuation_summaries: list of continuation-style summaries from the transcript

    ai_title entries look like:
        {"type": "ai-title", "aiTitle": "Fix flaky integration test in checkout flow"}

    Continuation summaries are user messages whose content starts with
    CONTINUATION_MARKER — rich structured summaries embedded in the transcript.
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


def _normalize_hosts(hosts: frozenset[str] | None) -> frozenset[str]:
    if hosts is None:
        return frozenset({"claude_code", "cursor"})
    return hosts


def bootstrap(
    verbose: bool = True,
    *,
    hosts: frozenset[str] | None = None,
) -> dict:
    """
    Import historical session data from selected agent hosts.

    Idempotent — already-captured sessions are skipped via ``sessions_captured``.

    ``hosts``:
      - ``None`` — both Claude Code and Cursor (when their transcript dirs exist)
      - ``frozenset({"claude_code"})`` / ``frozenset({"cursor"})`` — one host only
    """
    from brainvault.capture import process_session

    db.init_db()
    hosts_to = _normalize_hosts(hosts)

    stats: dict[str, int] = {
        "sessions_scanned": 0,
        "sessions_skipped": 0,
        "continuation_summaries": 0,
        "ai_titles": 0,
        "total_memories": 0,
        "cursor_sessions_scanned": 0,
        "cursor_sessions_skipped": 0,
        "cursor_memories_saved": 0,
    }

    if "claude_code" in hosts_to:
        for session_path in ClaudeCodeAdapter().iter_all_session_files():
            stats["sessions_scanned"] += 1

            if db.is_session_captured(str(session_path)):
                stats["sessions_skipped"] += 1
                continue

            project = extract_project_name(session_path)
            ai_title, summaries = _extract_session_data(session_path)

            saved = 0

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
                print(f"  [claude:{project}] {label}")

    if "cursor" in hosts_to:
        root = cursor_projects_dir()
        if root is not None and root.exists():
            adapter = CursorAdapter()
            for session_path in iter_cursor_transcript_paths_under(root):
                stats["cursor_sessions_scanned"] += 1
                sp = str(session_path)
                if db.is_session_captured(sp):
                    stats["cursor_sessions_skipped"] += 1
                    continue
                saved = process_session(session_path, adapter)
                stats["cursor_memories_saved"] += saved
                if verbose and saved > 0:
                    proj = adapter.extract_project_name(session_path)
                    print(f"  [cursor:{proj}] +{saved} memories")

    return stats


def run(hosts: frozenset[str] | None = None) -> None:
    """CLI entry point: ``brainvault bootstrap``."""
    hosts_to = _normalize_hosts(hosts)

    claude_root = claude_projects_dir()
    cursor_root = cursor_projects_dir()
    ok_any = False
    if "claude_code" in hosts_to and claude_root.exists():
        ok_any = True
    if "cursor" in hosts_to and cursor_root is not None and cursor_root.exists():
        ok_any = True

    if not ok_any:
        parts = []
        if "claude_code" in hosts_to:
            parts.append(f"Claude Code sessions: {claude_root} (missing)")
        if "cursor" in hosts_to:
            parts.append(
                f"Cursor transcripts: {cursor_root or CursorAdapter.SESSIONS_PATH} (missing)"
            )
        print("Nothing to import — no transcript directories found:\n")
        for p in parts:
            print(f"  · {p}")
        print()
        sys.exit(1)

    print("Importing session history…\n")
    if "claude_code" in hosts_to:
        print(f"  Claude Code: {claude_root}")
    if "cursor" in hosts_to:
        print(f"  Cursor:      {cursor_root or CursorAdapter.SESSIONS_PATH}")
    print()

    stats = bootstrap(verbose=True, hosts=hosts_to)

    new_claude = stats["sessions_scanned"] - stats["sessions_skipped"]

    print("\n  Done.")
    if "claude_code" in hosts_to:
        print(f"  · Claude — {stats['sessions_scanned']} sessions scanned")
        if stats["sessions_skipped"]:
            print(f"    · {stats['sessions_skipped']} already captured (skipped)")
        print(f"    · {new_claude} new sessions processed")
        print(f"    · {stats['continuation_summaries']} continuation summaries saved")
        print(f"    · {stats['ai_titles']} AI titles saved")
        print(f"    · {stats['total_memories']} total memories added (Claude)")
    if "cursor" in hosts_to:
        new_c = stats["cursor_sessions_scanned"] - stats["cursor_sessions_skipped"]
        print(f"  · Cursor — {stats['cursor_sessions_scanned']} transcripts scanned")
        if stats["cursor_sessions_skipped"]:
            print(f"    · {stats['cursor_sessions_skipped']} already captured (skipped)")
        print(f"    · {new_c} new transcripts processed")
        print(f"    · {stats['cursor_memories_saved']} memories saved (hook pipeline)")
    print("\n  Tip: run `brainvault embed` if you use semantic search.\n")


if __name__ == "__main__":
    run()

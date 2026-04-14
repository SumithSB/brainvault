"""
brainvault/installer.py — One-command setup for brainvault.
Patches ~/.claude/settings.json, creates ~/.claude/CLAUDE.md, registers Stop hook.

Usage:
    brainvault install    (via pyproject.toml entry point)
    python -m brainvault.installer
"""

import datetime
import json
import sys
from pathlib import Path

from brainvault import db


class SettingsJsonError(RuntimeError):
    """settings.json exists but is not valid JSON — refuse to overwrite user data."""


def _backup_corrupt_settings(path: Path) -> Path:
    """Copy path to a timestamped sibling before any repair attempt."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.brainvault-bak.{ts}")
    backup.write_bytes(path.read_bytes())
    return backup


def _load_claude_settings(path: Path) -> dict:
    """
    Parse settings.json. On invalid JSON, write a backup and raise SettingsJsonError.
    Root must be a JSON object (dict); other shapes are rejected to avoid data loss.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SettingsJsonError(f"Cannot read {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        backup = _backup_corrupt_settings(path)
        raise SettingsJsonError(
            f"{path} is not valid JSON ({e.msg} at line {e.lineno}). "
            f"A copy was saved to {backup}. Fix the file manually, then run brainvault install again."
        ) from e
    if not isinstance(data, dict):
        raise SettingsJsonError(
            f"{path} root must be a JSON object, got {type(data).__name__}. Refusing to modify."
        )
    return data


ENGRAM_MARKER = "<!-- brainvault-managed -->"
ENGRAM_END_MARKER = "<!-- /brainvault-managed -->"

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"

CLAUDE_MD_SNIPPET = f"""\
{ENGRAM_MARKER}
## Brainvault Memory

You have access to a personal memory store via the `brainvault` MCP tools.
This memory persists across all Claude Code sessions.

**CRITICAL: All brainvault operations happen silently in the background. NEVER ask the user
for confirmation before saving a memory, registering a project, or searching. Just do it.
Zero friction — context builds automatically from the natural flow of work.**

**At the start of every session:**
- Call `get_my_context()` silently — do not announce it or ask permission
- If the user mentions a specific project by name, also call `get_project(name)` silently

**During the session — do these proactively without being asked:**
- User requests anything → infer their role, stack, and preferences → `save_memory` type `profile` if new info
- User works on a project → `register_project` or update it silently
- User makes an architectural decision → `save_memory` type `decision` with full reasoning (not just the conclusion)
- User establishes a working pattern or preference → `save_memory` type `pattern`
- User corrects your approach or confirms a non-obvious choice → `save_memory` type `pattern`
- User says "update that memory", "that's no longer true", or "actually it's now..." → `update_memory`

**Before starting non-trivial work — always search first:**
- Auth, login, sessions, JWT, OAuth → `search_memory("auth")`
- Database schema, migrations, ORM choice → `search_memory("database")`
- API design, versioning, REST vs GraphQL → `search_memory("API")`
- Deployment, infra, CI/CD → `search_memory("deployment")`
- Any topic the user has mentioned before → `search_memory(<topic>)`

**When user asks "do you remember..." or "we discussed...":**
- Call `search_memory` before answering — never guess from context alone

**Closing the feedback loop — call `record_outcome` when:**
- A feature built on a past decision shipped successfully → sentiment: "positive"
- A past architectural choice caused bugs or was reverted → sentiment: "negative"
- A decision had mixed results or needed adjustment → sentiment: "mixed"
- User says "that worked", "that failed", "we had to change X because..."

**Periodic reflection — call `reflect()` when:**
- User asks "what patterns do I repeat?" or "what are my gaps?"
- Starting a new project and want to apply lessons from past ones
- User mentions the same problem area that appeared in previous projects

Save the *reasoning*, not just the conclusion. "Used Celery" is weak. "Used Celery over RQ because
the report pipeline needs canvas workflows and retry budgets per task — RQ can't express that" is strong.
{ENGRAM_END_MARKER}
"""


def _get_mcp_entry() -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "brainvault.mcp_server"],
    }


def _get_stop_hook_entry() -> dict:
    # Quote sys.executable so paths with spaces (e.g. /Users/John Doe/venv/bin/python)
    # are passed as a single token to the shell.
    exe = sys.executable.replace('"', '\\"')
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": f'"{exe}" -m brainvault.capture',
            }
        ],
    }


def _get_post_tool_hook_entry() -> dict:
    exe = sys.executable.replace('"', '\\"')
    return {
        "matcher": "Write|Edit|Bash|TodoWrite|NotebookEdit",
        "hooks": [
            {
                "type": "command",
                "command": f'"{exe}" -m brainvault.tool_capture',
            }
        ],
    }


def _patch_claude_settings() -> tuple[bool, bool, bool]:
    """
    Add mcpServers.brainvault, Stop hook, and PostToolUse hook to settings.json.
    Returns (mcp_added, stop_hook_added, post_tool_hook_added).

    Raises SettingsJsonError if the file exists but cannot be parsed — never overwrites
    with an empty object.
    """
    data = _load_claude_settings(CLAUDE_SETTINGS) if CLAUDE_SETTINGS.exists() else {}

    mcp_added = False
    hook_added = False
    post_hook_added = False

    # MCP server entry
    data.setdefault("mcpServers", {})
    if "brainvault" not in data["mcpServers"]:
        data["mcpServers"]["brainvault"] = _get_mcp_entry()
        mcp_added = True

    data.setdefault("hooks", {})

    # Stop hook entry
    data["hooks"].setdefault("Stop", [])
    stop_cmds = [
        h.get("command", "") for entry in data["hooks"]["Stop"] for h in entry.get("hooks", [])
    ]
    if not any("brainvault.capture" in cmd for cmd in stop_cmds):
        data["hooks"]["Stop"].append(_get_stop_hook_entry())
        hook_added = True

    # PostToolUse hook entry
    data["hooks"].setdefault("PostToolUse", [])
    post_cmds = [
        h.get("command", "")
        for entry in data["hooks"]["PostToolUse"]
        for h in entry.get("hooks", [])
    ]
    if not any("brainvault.tool_capture" in cmd for cmd in post_cmds):
        data["hooks"]["PostToolUse"].append(_get_post_tool_hook_entry())
        post_hook_added = True

    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2))
    return mcp_added, hook_added, post_hook_added


def _patch_claude_md() -> str:
    """
    Inject or upgrade Brainvault instructions in ~/.claude/CLAUDE.md.

    - First install: appends snippet to any existing content.
    - Re-install / upgrade: replaces the brainvault block in-place so new
      instructions are always current without touching surrounding user content.

    Returns one of: "injected" | "upgraded" | "current"
    """
    existing = CLAUDE_MD.read_text() if CLAUDE_MD.exists() else ""

    if ENGRAM_MARKER not in existing:
        # Fresh install — append
        CLAUDE_MD.write_text(existing + ("\n\n" if existing else "") + CLAUDE_MD_SNIPPET)
        return "injected"

    # Already present — replace the block so upgrades take effect.
    # Support both old format (no end marker) and new format (with end marker).
    if ENGRAM_END_MARKER in existing:
        start = existing.index(ENGRAM_MARKER)
        end = existing.index(ENGRAM_END_MARKER) + len(ENGRAM_END_MARKER)
        # Absorb any trailing newline after the end marker
        if end < len(existing) and existing[end] == "\n":
            end += 1
    else:
        # Old format: no end marker — replace from start marker to end of string
        start = existing.index(ENGRAM_MARKER)
        end = len(existing)

    current_block = existing[start:end].rstrip()
    if current_block == CLAUDE_MD_SNIPPET.rstrip():
        return "current"

    before = existing[:start]
    after = existing[end:].lstrip("\n")
    separator = "\n\n" if after else ""
    CLAUDE_MD.write_text(before + CLAUDE_MD_SNIPPET + separator + after)
    return "upgraded"


def _seed_vault() -> None:
    """
    Automatically seed the vault on first install:
      1. Scan all git repos under ~/ for architectural decision memories
      2. Import past Claude Code session summaries

    Both steps run unconditionally. Press Ctrl+C to skip either one.
    In non-interactive environments (CI, pipes) the scan still runs — only
    the live progress counter is suppressed.
    """
    import datetime

    from brainvault.git_scan import discover_repos, scan_repo

    print("  Seeding vault from git history (Ctrl+C to skip)…\n")
    try:
        root = Path.home()
        since = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
        repos = discover_repos(root, progress=sys.stdout.isatty())
        print(f"\n  Found {len(repos)} repos. Scanning (last 12 months, limit 200 commits each)…\n")
        total_saved = 0
        for repo in repos:
            project = repo.name
            try:
                stats = scan_repo(repo, project=project, since=since, limit=200, verbose=False)
                saved = stats["commits_saved"]
                if saved:
                    print(f"  {repo}  →  {saved} memories saved")
                    total_saved += saved
            except Exception as e:
                print(f"  (skipped {repo}: {e})", file=sys.stderr)
        print(
            f"\n  ✓ Git scan complete — {total_saved} memories saved across {len(repos)} repos.\n"
        )
    except KeyboardInterrupt:
        print("\n  Skipped. Run 'brainvault bootstrap-git ~/' any time.\n")
        return

    print("  Importing past Claude Code session summaries (Ctrl+C to skip)…\n")
    try:
        from brainvault.bootstrap import run as bootstrap_run

        bootstrap_run()
    except KeyboardInterrupt:
        print("\n  Skipped. Run 'brainvault bootstrap' any time.\n")


def install() -> None:
    print("Installing brainvault...\n")

    # 1. Create ~/.brainvault/ and init DB
    db.init_db()
    db_path = db.get_db_path()
    print(f"  ✓ Database initialised at {db_path}")

    # 2. Patch Claude Code settings
    if not CLAUDE_SETTINGS.exists():
        print(f"  ✗ Claude Code settings not found at {CLAUDE_SETTINGS}")
        print("    Please ensure Claude Code is installed and has been run at least once.")
        sys.exit(1)

    try:
        mcp_added, hook_added, post_hook_added = _patch_claude_settings()
    except SettingsJsonError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)
    if mcp_added:
        print(f"  ✓ MCP server registered in {CLAUDE_SETTINGS}")
    else:
        print("  · MCP server already registered (skipped)")

    if hook_added:
        print(f"  ✓ Stop hook registered in {CLAUDE_SETTINGS}")
    else:
        print("  · Stop hook already registered (skipped)")

    if post_hook_added:
        print(f"  ✓ PostToolUse hook registered in {CLAUDE_SETTINGS}")
    else:
        print("  · PostToolUse hook already registered (skipped)")

    # 3. Patch CLAUDE.md
    md_result = _patch_claude_md()
    if md_result == "injected":
        print(f"  ✓ Instructions injected into {CLAUDE_MD}")
    elif md_result == "upgraded":
        print(f"  ✓ Instructions upgraded in {CLAUDE_MD}")
    else:
        print("  · CLAUDE.md already up to date (skipped)")

    print("\n  Done. Restart Claude Code to activate brainvault.\n")
    print("  What happens next:")
    print("  - Every session will start with your personal context loaded")
    print("  - Say 'remember this' and Claude will save it to your vault")
    print("  - Continuation summaries are auto-captured after each session\n")

    _seed_vault()


if __name__ == "__main__":
    install()

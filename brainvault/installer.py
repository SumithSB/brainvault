"""
brainvault/installer.py — One-command setup for brainvault.
Patches ~/.claude/settings.json, creates ~/.claude/CLAUDE.md, registers Stop hook.

Usage:
    brainvault install    (via pyproject.toml entry point)
    python -m brainvault.installer
"""

import json
import sys
from pathlib import Path

from brainvault import db

ENGRAM_MARKER = "<!-- brainvault-managed -->"

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"

CLAUDE_MD_SNIPPET = f"""\
{ENGRAM_MARKER}
## Brainvault Memory

You have access to a personal memory store via the `brainvault` MCP tools.
This memory persists across all Claude Code sessions.

**At the start of every session:**
- Call `get_my_context()` to load who the user is and what projects exist
- If the user mentions a specific project by name, also call `get_project(name)`

**During the session:**
- User says "remember this", "save this", "note this" → call `save_memory`
- User describes themselves, their preferences, or working style → `save_memory` with type `profile`
- User describes a new project → call `register_project`
- User makes an architectural decision with reasoning → `save_memory` with type `decision`
- User establishes a convention or pattern → `save_memory` with type `pattern`
- Before implementing auth, database design, API structure, or deployment → call `search_memory`

**When user asks "do you remember..." or "we discussed...":**
- Call `search_memory` before answering

Save the *reasoning*, not just the conclusion. "Used JWT" is weak. "Used JWT over sessions because
the API needs to be stateless for horizontal scaling" is strong.
"""


def _get_mcp_entry() -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "brainvault.mcp_server"],
    }


def _get_stop_hook_entry() -> dict:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": f"{sys.executable} -m brainvault.capture",
            }
        ],
    }


def _patch_claude_settings() -> tuple[bool, bool]:
    """
    Add mcpServers.brainvault and hooks.Stop entry to ~/.claude/settings.json.
    Returns (mcp_added, hook_added).
    """
    data = {}
    if CLAUDE_SETTINGS.exists():
        try:
            data = json.loads(CLAUDE_SETTINGS.read_text())
        except json.JSONDecodeError:
            data = {}

    mcp_added = False
    hook_added = False

    # MCP server entry
    data.setdefault("mcpServers", {})
    if "brainvault" not in data["mcpServers"]:
        data["mcpServers"]["brainvault"] = _get_mcp_entry()
        mcp_added = True

    # Stop hook entry
    data.setdefault("hooks", {})
    data["hooks"].setdefault("Stop", [])
    existing_commands = [
        h.get("command", "") for entry in data["hooks"]["Stop"] for h in entry.get("hooks", [])
    ]
    if not any("brainvault.capture" in cmd for cmd in existing_commands):
        data["hooks"]["Stop"].append(_get_stop_hook_entry())
        hook_added = True

    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2))
    return mcp_added, hook_added


def _patch_claude_md() -> bool:
    """
    Inject Brainvault instructions into ~/.claude/CLAUDE.md.
    Idempotent — skips if marker already present.
    Returns True if injected.
    """
    existing = CLAUDE_MD.read_text() if CLAUDE_MD.exists() else ""
    if ENGRAM_MARKER in existing:
        return False
    CLAUDE_MD.write_text(existing + ("\n\n" if existing else "") + CLAUDE_MD_SNIPPET)
    return True


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

    mcp_added, hook_added = _patch_claude_settings()
    if mcp_added:
        print(f"  ✓ MCP server registered in {CLAUDE_SETTINGS}")
    else:
        print("  · MCP server already registered (skipped)")

    if hook_added:
        print(f"  ✓ Stop hook registered in {CLAUDE_SETTINGS}")
    else:
        print("  · Stop hook already registered (skipped)")

    # 3. Patch CLAUDE.md
    injected = _patch_claude_md()
    if injected:
        print(f"  ✓ Instructions injected into {CLAUDE_MD}")
    else:
        print("  · CLAUDE.md already configured (skipped)")

    print("\n  Done. Restart Claude Code to activate brainvault.\n")
    print("  What happens next:")
    print("  - Every session will start with your personal context loaded")
    print("  - Say 'remember this' and Claude will save it to your vault")
    print("  - Continuation summaries are auto-captured after each session")
    print("\n  Tip: seed your vault from existing conversation history:")
    print("       brainvault bootstrap\n")


if __name__ == "__main__":
    install()

"""
brainvault/installer.py — Adapter-dispatched install / uninstall.

The host-specific filesystem work (Claude Code settings.json, Cursor mcp.json,
rules files, CLAUDE.md) lives in `brainvault.adapters`. This module:

  - Resolves which adapters to target (auto-detect installed, or explicit list)
  - Runs MCP registration, hook registration, instruction injection per adapter
  - Prints a compact progress line per step
  - Auto-seeds the vault on first install when the vault is empty (git scan + session bootstrap)

Re-exports `SettingsJsonError` and marker constants from `brainvault.adapters.claude_code`
for callers that imported them from this module.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from brainvault import db
from brainvault.adapters import AgentAdapter, installed_adapters, resolve
from brainvault.adapters.claude_code import (
    ENGRAM_END_MARKER,
    ENGRAM_MARKER,
    SettingsJsonError,
    _backup_corrupt_settings,
)

# ---------------------------------------------------------------------------
# Auto-seed (runs on first install — empty vault only; see install())
# ---------------------------------------------------------------------------


def _seed_vault() -> None:
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


# ---------------------------------------------------------------------------
# Adapter-dispatched install / uninstall
# ---------------------------------------------------------------------------


def _resolve_targets(agents: list[str] | None) -> list[AgentAdapter]:
    """
    Pick adapters to act on.

    - None / unset → every installed host is patched
    - ["all"]      → every known host, even if not detected (useful for scripts)
    - explicit names → only those (unknown names raise via adapters.resolve)
    """
    targets = resolve(agents) if agents is not None else installed_adapters()
    return targets


def _install_one(adapter: AgentAdapter) -> None:
    print(f"  [{adapter.display_name}]")

    if not adapter.is_installed():
        print(f"    · not detected — skipping {adapter.display_name}")
        return

    try:
        if adapter.register_mcp():
            print("    ✓ MCP server registered")
        else:
            print("    · MCP server already registered (skipped)")
    except SettingsJsonError as e:
        print(f"    ✗ {e}", file=sys.stderr)
        return

    hook_res = adapter.register_hooks()
    for h in hook_res.registered:
        print(f"    ✓ {h} hook registered")
    for h in hook_res.skipped:
        print(f"    · {h} hook already registered (skipped)")

    status = adapter.inject_instructions()
    if status == "injected":
        print("    ✓ Instructions injected")
    elif status == "upgraded":
        print("    ✓ Instructions upgraded")
    elif status == "current":
        print("    · Instructions already up to date (skipped)")
    else:
        print(f"    · Instructions: {status}")


def _uninstall_one(adapter: AgentAdapter) -> None:
    print(f"  [{adapter.display_name}]")

    if not adapter.is_installed():
        print(f"    · not detected — skipping {adapter.display_name}")
        return

    try:
        if adapter.unregister_mcp():
            print("    ✓ MCP server removed")
        else:
            print("    · MCP server was not registered (skipped)")
    except SettingsJsonError as e:
        print(f"    ✗ {e}", file=sys.stderr)
        return

    hook_res = adapter.unregister_hooks()
    for h in hook_res.removed:
        print(f"    ✓ {h} hook removed")
    for h in hook_res.skipped:
        print(f"    · {h} hook was not registered (skipped)")

    status = adapter.strip_instructions()
    if status == "removed":
        print("    ✓ Instructions removed")
    elif status == "not-present":
        print("    · Instructions block was not present (skipped)")
    elif status == "missing-file":
        print("    · Instructions file not found (skipped)")
    else:
        print(f"    · Instructions: {status}")


def install(agents: list[str] | None = None) -> None:
    """Run adapter install across every detected host (or an explicit subset)."""
    print("Installing brainvault...\n")

    db.init_db()
    db_path = db.get_db_path()
    print(f"  ✓ Database initialised at {db_path}\n")

    try:
        targets = _resolve_targets(agents)
    except ValueError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)

    if not targets:
        print("  ✗ No supported coding agents detected.")
        print("    Install Claude Code or Cursor first, then run `brainvault install` again.")
        sys.exit(1)

    for adapter in targets:
        _install_one(adapter)
        print()

    print("  Done. Restart your coding agent to activate brainvault.\n")
    print("  What happens next:")
    print("  - Every session will start with your personal context loaded")
    print("  - Say 'remember this' and your agent will save it to your vault")
    print("  - Session notes are auto-captured after each agent stop (Claude Code + Cursor)\n")

    if db.get_stats()["total_memories"] == 0:
        _seed_vault()
    else:
        print(
            "  Vault already populated; skipping auto-seed "
            "(run 'brainvault bootstrap-git' / 'brainvault bootstrap' to re-seed).\n"
        )


def uninstall(*, purge: bool = False, agents: list[str] | None = None) -> None:
    """Reverse everything install() did for the given (or detected) adapters."""
    import shutil

    print("Uninstalling brainvault...\n")

    try:
        targets = _resolve_targets(agents)
    except ValueError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)

    if not targets:
        print("  · No supported coding agents detected — nothing to uninstall.")
    else:
        for adapter in targets:
            _uninstall_one(adapter)
            print()

    if purge:
        vault_dir = db.get_db_path().parent
        if vault_dir.exists():
            shutil.rmtree(vault_dir)
            print(f"  ✓ Vault directory deleted: {vault_dir}")
        else:
            print(f"  · Vault directory not found at {vault_dir} (skipped)")
    else:
        db_path = db.get_db_path()
        if db_path.exists():
            print(f"  Vault preserved at {db_path}")
            print("  Pass --purge to delete the database and cached data.")

    print("\n  Done. Restart your coding agent for the change to take effect.\n")


__all__ = [
    "SettingsJsonError",
    "ENGRAM_MARKER",
    "ENGRAM_END_MARKER",
    "_backup_corrupt_settings",
    "install",
    "uninstall",
]


if __name__ == "__main__":
    install()

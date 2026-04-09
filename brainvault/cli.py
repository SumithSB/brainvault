"""
brainvault/cli.py — Command-line interface for brainvault.

Commands:
    brainvault install    — set up MCP server + Stop hook in Claude Code
    brainvault bootstrap  — seed memory from existing Claude Code session history
    brainvault search     — search stored memories
    brainvault stats      — show memory statistics
"""

import sys


def main() -> None:
    if len(sys.argv) < 2:
        _print_usage()
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "install":
        from brainvault.installer import install

        install()

    elif cmd == "init":
        _cmd_init()

    elif cmd == "bootstrap":
        from brainvault.bootstrap import run as bootstrap_run

        bootstrap_run()

    elif cmd == "search":
        _cmd_search()

    elif cmd == "stats":
        _cmd_stats()

    else:
        print(f"Unknown command: {cmd}")
        _print_usage()
        sys.exit(1)


def _print_usage() -> None:
    print("Usage: brainvault <command>")
    print()
    print("Commands:")
    print("  install    Set up MCP server and Stop hook in Claude Code")
    print("  init       Onboard a new project with structured prompts")
    print("  bootstrap  Seed memory from existing Claude Code session history")
    print("  search     Search stored memories")
    print("  stats      Show memory statistics")


def _cmd_init() -> None:
    """Structured onboarding for a new project."""
    print("Brainvault project init\n")

    name = input("Project name (short identifier, e.g. 'pluto-api'): ").strip()
    if not name:
        print("Project name cannot be empty.")
        return

    description = input("What does it do? (1-2 sentences): ").strip()
    stack_raw = input("Tech stack (comma-separated, e.g. 'FastAPI, PostgreSQL, Redis'): ").strip()
    stack = [s.strip() for s in stack_raw.split(",") if s.strip()]

    print("\nOptional — press Enter to skip:")
    goals = input("Goals / intended outcomes: ").strip()
    constraints = input("Key constraints (budget, timeline, compliance, etc.): ").strip()
    alternatives = input("Alternatives you considered (and why you rejected them): ").strip()

    from brainvault import db

    db.init_db()
    notes_parts = []
    if goals:
        notes_parts.append(f"Goals: {goals}")
    if constraints:
        notes_parts.append(f"Constraints: {constraints}")
    notes = " | ".join(notes_parts)

    db.save_project(name=name, description=description, stack=stack, notes=notes)

    if alternatives:
        db.save_memory(
            content=f"Alternatives considered for {name}: {alternatives}",
            memory_type="decision",
            project=name,
            source="explicit",
        )

    print(f"\nProject '{name}' registered.")
    if alternatives:
        print("Alternatives saved as a decision memory.")
    print("\nTip: run 'brainvault search <topic>' to verify your memories are searchable.")


def _cmd_search() -> None:
    args = sys.argv[2:]
    if not args:
        print("Usage: brainvault search <query> [--project <name>]")
        sys.exit(1)

    project = None
    query_parts = []
    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        else:
            query_parts.append(args[i])
            i += 1

    query = " ".join(query_parts)

    from brainvault import db

    db.init_db()
    results = db.search_memories(query, project=project, limit=10)

    if not results:
        print(f'No memories found for: "{query}"')
        return

    import json

    print(f'Found {len(results)} memories for "{query}":\n')
    for i, m in enumerate(results, 1):
        proj = m["project"] or "global"
        keywords = json.loads(m["keywords"]) if isinstance(m["keywords"], str) else m["keywords"]
        content = m["content"]
        if len(content) > 200:
            content = content[:200].rstrip() + "…"
        print(f"[{i}] {m['memory_type']} · project: {proj}")
        print(f"    {content}")
        if keywords:
            print(f"    Keywords: {', '.join(keywords[:6])}")
        print()


def _cmd_stats() -> None:
    from brainvault import db

    db.init_db()
    stats = db.get_stats()

    print("Brainvault statistics\n")
    print(f"  Total memories : {stats['total_memories']}")
    print(f"  Total projects : {stats['total_projects']}")

    if stats["by_type"]:
        print("\n  By type:")
        for t, count in sorted(stats["by_type"].items()):
            print(f"    {t:<12} {count}")

    if stats["by_project"]:
        print("\n  By project:")
        for proj, count in sorted(stats["by_project"].items(), key=lambda x: -x[1]):
            print(f"    {proj:<20} {count}")

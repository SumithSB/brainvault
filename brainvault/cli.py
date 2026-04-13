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

    elif cmd == "embed":
        _cmd_embed()

    elif cmd == "git-scan":
        _cmd_git_scan()

    elif cmd == "status":
        _cmd_status()

    elif cmd == "update":
        _cmd_update()

    elif cmd == "graph":
        _cmd_graph()

    elif cmd == "reflect":
        _cmd_reflect()

    elif cmd == "forget":
        _cmd_forget()

    elif cmd == "index-repo":
        _cmd_index_repo()

    elif cmd == "bootstrap-git":
        _cmd_bootstrap_git()

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
    print("  embed      Backfill semantic embeddings for all stored memories")
    print("  git-scan      Mine git history for architectural decision memories")
    print("  bootstrap-git Discover and scan all local git repos under a path")
    print("  index-repo    Index file structure and co-change matrix for a repo")
    print("  bootstrap  Seed memory from existing Claude Code session history")
    print("  search     Search stored memories")
    print("  status     Show vault health at a glance")
    print("  update     Edit an existing memory by ID")
    print("  graph      Generate HTML brain graph of all memories")
    print("  reflect    Surface cross-project patterns and open decisions")
    print("  forget     Delete a memory by ID")
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


def _cmd_embed() -> None:
    """Backfill semantic embeddings for all memories that don't have one yet."""
    try:
        from brainvault import embeddings as emb

        if not emb._is_available():
            print("Error: semantic extras not installed.")
            print("Run: pip install 'brainvault[semantic]'")
            sys.exit(1)
    except ImportError:
        print("Error: semantic extras not installed.")
        print("Run: pip install 'brainvault[semantic]'")
        sys.exit(1)

    from brainvault import db

    db.init_db()
    pending = db.get_unembedded_memories()

    if not pending:
        already = db.count_embedded()
        print(f"All {already} memories already have embeddings. Nothing to do.")
        return

    print(f"Embedding {len(pending)} memories...")
    print("(First run downloads BAAI/bge-small-en-v1.5 ~130MB to ~/.cache/huggingface)\n")

    BATCH = 32
    total = len(pending)
    embedded = 0

    for i in range(0, total, BATCH):
        batch = pending[i : i + BATCH]
        texts = [m["content"] for m in batch]
        ids = [m["id"] for m in batch]
        vectors = emb.embed_batch(texts)
        for mid, vec in zip(ids, vectors):
            db.store_embedding(mid, vec)
        embedded += len(batch)
        pct = int(embedded / total * 100)
        print(f"  [{pct:3d}%] {embedded}/{total}", end="\r", flush=True)

    print(f"\nDone. {embedded} memories embedded.")
    print(f"Total embedded: {db.count_embedded()}")


def _cmd_index_repo() -> None:
    """Index a repository's file structure and co-change matrix."""
    from pathlib import Path

    from brainvault import code_scan, db, git_scan

    args = sys.argv[2:]
    path_str: str | None = None
    project: str | None = None
    min_cochange: int = 2

    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        elif args[i] == "--min-cochange" and i + 1 < len(args):
            try:
                min_cochange = int(args[i + 1])
            except ValueError:
                print(f"Error: --min-cochange must be an integer, got '{args[i + 1]}'")
                sys.exit(1)
            i += 2
        elif not args[i].startswith("--"):
            if path_str is None:
                path_str = args[i]
            i += 1
        else:
            print(f"Unknown argument: {args[i]}")
            print("Usage: brainvault index-repo [path] [--project <name>] [--min-cochange <n>]")
            sys.exit(1)

    repo_path = Path(path_str).expanduser() if path_str else Path.cwd()

    try:
        resolved = git_scan._resolve_repo_path(repo_path)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not project:
        project = resolved.name

    print(f"Indexing: {resolved}")
    print(f"Project:  {project}\n")

    db.init_db()

    try:
        stats = code_scan.index_repo(
            repo_path=resolved,
            project=project,
            min_cochange=min_cochange,
            verbose=True,
        )
    except Exception as e:
        print(f"\nError during indexing: {e}")
        sys.exit(1)

    print("\nDone.")
    print(
        f"  {stats['files_found']} files indexed  ({', '.join(f'{lang}:{n}' for lang, n in sorted(stats['languages'].items()))})"
    )
    print(f"  {stats['cochange_pairs']} co-change pairs")
    if stats["parse_errors"]:
        print(f"  {stats['parse_errors']} files with parse errors (skipped gracefully)")
    print("\nTip: run 'brainvault graph --open' to visualise the structure.")


def _cmd_bootstrap_git() -> None:
    """Discover and scan all git repos under a root directory."""
    import datetime
    from pathlib import Path

    from brainvault import db, git_scan

    args = sys.argv[2:]
    root_str: str | None = None
    since_str: str | None = None
    limit_per_repo: int = 200
    dry_run: bool = False

    i = 0
    while i < len(args):
        if args[i] == "--since" and i + 1 < len(args):
            since_str = args[i + 1]
            i += 2
        elif args[i] == "--limit-per-repo" and i + 1 < len(args):
            try:
                limit_per_repo = int(args[i + 1])
            except ValueError:
                print(f"Error: --limit-per-repo must be an integer, got '{args[i + 1]}'")
                sys.exit(1)
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif not args[i].startswith("--"):
            if root_str is None:
                root_str = args[i]
            i += 1
        else:
            print(f"Unknown argument: {args[i]}")
            print(
                "Usage: brainvault bootstrap-git [root] [--since <date>] [--limit-per-repo <n>] [--dry-run]"
            )
            sys.exit(1)

    root = Path(root_str).expanduser() if root_str else Path.home()

    print(f"Root: {root}")
    if not root_str:
        print("(Tip: pass a path to narrow the search, e.g. brainvault bootstrap-git ~/Projects)\n")

    if since_str:
        try:
            since = datetime.datetime.fromisoformat(since_str).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            print(f"Error: --since must be an ISO date like '2023-01-01', got '{since_str}'")
            sys.exit(1)
    else:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365 * 2)

    print(f"Discovering git repos under: {root}")
    repos = git_scan.discover_repos(root, progress=True)

    if not repos:
        print("No git repositories found.")
        return

    print(f"Found {len(repos)} repositories.\n")

    if dry_run:
        print("Dry run — repos that would be scanned:\n")
        for r in repos:
            print(f"  {r}")
        return

    db.init_db()

    total_examined = 0
    total_saved = 0
    total_skipped = 0

    for idx, repo in enumerate(repos, 1):
        project = repo.name
        print(f"[{idx}/{len(repos)}] {repo.name} ({repo})")

        try:
            stats = git_scan.scan_repo(
                repo_path=repo,
                project=project,
                since=since,
                limit=limit_per_repo,
                verbose=False,
            )
        except Exception as e:
            print(f"  Error: {e}")
            continue

        total_examined += stats["commits_examined"]
        total_saved += stats["commits_saved"]
        total_skipped += stats["already_scanned"]

        parts = []
        if stats["commits_saved"]:
            parts.append(f"{stats['commits_saved']} memories saved")
        if stats["already_scanned"]:
            parts.append(f"{stats['already_scanned']} already scanned")
        if stats["not_significant"]:
            parts.append(f"{stats['not_significant']} filtered")
        print(
            f"  {stats['commits_examined']} commits — "
            + (", ".join(parts) if parts else "nothing new")
        )

        # Auto-index file structure — failure must never interrupt bootstrap
        try:
            from brainvault import code_scan

            idx = code_scan.index_repo(repo_path=repo, project=project, verbose=False)
            if idx["files_found"]:
                print(
                    f"  indexed {idx['files_found']} files, {idx['cochange_pairs']} co-change pairs"
                )
        except Exception as e:
            print(f"  (code index skipped: {e})")

    print("\nDone.")
    print(f"  {len(repos)} repos scanned")
    print(f"  {total_examined} commits examined")
    print(f"  {total_saved} memories saved")
    if total_skipped:
        print(f"  {total_skipped} already in vault (skipped)")


def _cmd_git_scan() -> None:
    """Mine a project's git history for architectural decision memories."""
    import datetime
    from pathlib import Path

    args = sys.argv[2:]

    path_str: str | None = None
    project: str | None = None
    since_str: str | None = None
    limit: int = 500

    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        elif args[i] == "--since" and i + 1 < len(args):
            since_str = args[i + 1]
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print(f"Error: --limit must be an integer, got '{args[i + 1]}'")
                sys.exit(1)
            i += 2
        elif not args[i].startswith("--"):
            if path_str is None:
                path_str = args[i]
            i += 1
        else:
            print(f"Unknown argument: {args[i]}")
            print(
                "Usage: brainvault git-scan [path] [--project <name>] [--since <date>] [--limit <n>]"
            )
            sys.exit(1)

    repo_path = Path(path_str) if path_str else Path.cwd()

    if not project:
        project = repo_path.resolve().name

    if since_str:
        try:
            since = datetime.datetime.fromisoformat(since_str).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            print(f"Error: --since must be an ISO date like '2023-01-01', got '{since_str}'")
            sys.exit(1)
    else:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)

    from brainvault import db, git_scan

    db.init_db()

    try:
        resolved = git_scan._resolve_repo_path(repo_path)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Scanning git history in: {resolved}")
    print(f"Project: {project}")
    print(f"Since:   {since.date()}")
    print(f"Limit:   {limit} commits\n")

    try:
        stats = git_scan.scan_repo(
            repo_path=resolved,
            project=project,
            since=since,
            limit=limit,
            verbose=True,
        )
    except Exception as e:
        print(f"\nError during git scan: {e}")
        sys.exit(1)

    print("Done.")
    print(f"  · {stats['commits_examined']} commits examined")
    if stats["already_scanned"]:
        print(f"  · {stats['already_scanned']} already scanned (skipped)")
    print(f"  · {stats['not_significant']} filtered as not significant")
    print(f"  · {stats['commits_saved']} memories saved to project '{project}'")


def _cmd_status() -> None:
    """Show vault health at a glance."""
    from brainvault import db

    db.init_db()
    s = db.get_status()

    print("Brainvault status\n")
    print(f"  Memories      : {s['total_memories']}")

    if s["by_type"]:
        for t, count in sorted(s["by_type"].items()):
            print(f"    {t:<12} {count}")

    if s["by_source"]:
        print("\n  By source:")
        for src, count in sorted(s["by_source"].items()):
            print(f"    {src:<12} {count}")

    if s["unembedded"]:
        print(f"\n  Unembedded    : {s['unembedded']}  (run 'brainvault embed' to fix)")
    else:
        print("\n  Embeddings    : all up to date")

    if s["git_repos"]:
        print(f"\n  Git repos scanned : {s['git_repos']}")
        print(f"  Git memories      : {s['git_memories']}")

    if s["last_session_at"]:
        print(
            f"\n  Last session  : {s['last_session_at']} ({s['last_session_memories']} memories captured)"
        )

    if s["open_decisions"]:
        print(
            f"\n  Open decisions: {s['open_decisions']}  (unresolved, >7 days old — run 'brainvault reflect')"
        )
    if s["stale_projects"]:
        print(f"  Stale projects: {s['stale_projects']}  (active but idle >30 days)")


def _cmd_update() -> None:
    """Edit an existing memory by ID."""
    args = sys.argv[2:]
    if not args:
        print("Usage: brainvault update <id> [--content <text>] [--type <type>] [--project <name>]")
        sys.exit(1)

    memory_id = args[0]
    new_content: str | None = None
    new_type: str | None = None
    new_project: str | None = None

    i = 1
    while i < len(args):
        if args[i] == "--content" and i + 1 < len(args):
            new_content = args[i + 1]
            i += 2
        elif args[i] == "--type" and i + 1 < len(args):
            new_type = args[i + 1]
            i += 2
        elif args[i] == "--project" and i + 1 < len(args):
            new_project = args[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {args[i]}")
            print(
                "Usage: brainvault update <id> [--content <text>] [--type <type>] [--project <name>]"
            )
            sys.exit(1)

    if new_content is None and new_type is None and new_project is None:
        print("Error: provide at least one of --content, --type, --project")
        sys.exit(1)

    from brainvault import db

    if new_type and new_type not in db.VALID_MEMORY_TYPES:
        print(
            f"Error: invalid type '{new_type}'. Must be one of: {', '.join(sorted(db.VALID_MEMORY_TYPES))}"
        )
        sys.exit(1)

    db.init_db()
    updated = db.update_memory(
        memory_id,
        content=new_content,
        memory_type=new_type,
        project=new_project,
    )
    if updated:
        parts = []
        if new_content:
            parts.append("content")
        if new_type:
            parts.append("type")
        if new_project:
            parts.append("project")
        print(f"Memory {memory_id} updated ({', '.join(parts)}).")
    else:
        print(f"Memory {memory_id} not found.")
        sys.exit(1)


def _cmd_graph() -> None:
    """Generate a self-contained HTML brain graph of all memories."""
    import webbrowser
    from pathlib import Path

    args = sys.argv[2:]
    output: str | None = None
    open_browser = False

    i = 0
    while i < len(args):
        if args[i] == "--output" and i + 1 < len(args):
            output = args[i + 1]
            i += 2
        elif args[i] == "--open":
            open_browser = True
            i += 1
        else:
            print(f"Unknown argument: {args[i]}")
            print("Usage: brainvault graph [--output <path>] [--open]")
            sys.exit(1)

    out_path = Path(output) if output else Path.home() / ".brainvault" / "graph.html"

    from brainvault import graph

    result = graph.generate(out_path)
    print(f"Graph written to: {result}")
    print("Open in any browser to explore your memory vault.")

    if open_browser:
        webbrowser.open(result.as_uri())


def _cmd_reflect() -> None:
    """Surface cross-project patterns, open decisions, and knowledge gaps."""
    from brainvault import db

    db.init_db()
    data = db.get_reflection_data()

    print("Brainvault reflection\n")

    open_decisions = data["open_decisions"]
    if open_decisions:
        print(f"Open decisions ({len(open_decisions)} unresolved, >7 days old):\n")
        for d in open_decisions:
            proj = d["project"] or "global"
            created = d["created_at"][:10] if d["created_at"] else "?"
            snippet = d["content"][:100] + ("..." if len(d["content"]) > 100 else "")
            print(f"  [{proj}] {snippet}")
            print(f"         ID: {d['id']}  decided: {created}")
        print()
    else:
        print("Open decisions: none — all decisions have recorded outcomes.\n")

    patterns = data["cross_project_patterns"]
    if patterns:
        print("Cross-project patterns (topics appearing in 2+ projects):\n")
        for kw, projs in patterns:
            print(f"  {kw:<20} {', '.join(projs)}")
        print()

    stale = data["stale_projects"]
    if stale:
        print("Stale projects (active, idle >30 days):\n")
        for p in stale:
            last = (p.get("last_active") or p.get("updated_at") or "")[:10]
            print(f"  {p['name']:<20} last active: {last}")
        print()

    hot = data["hot_memories"]
    if hot:
        print("Most accessed memories:\n")
        for m in hot:
            proj = m["project"] or "global"
            snippet = m["content"][:80] + ("..." if len(m["content"]) > 80 else "")
            print(f"  [{m['memory_type']} · {proj}] {snippet}")
            print(f"  accessed {m['access_count']} times")
        print()

    sentiment = data.get("outcome_sentiment_summary", {})
    if sentiment:
        total = sum(sentiment.values())
        print(f"Decision outcomes ({total} recorded):\n")
        for label in ("positive", "negative", "mixed", "unrated"):
            count = sentiment.get(label, 0)
            if count:
                bar = "#" * min(count, 20)
                print(f"  {label:<10} {bar} {count}")
        print()

    if not any([open_decisions, patterns, stale, hot, sentiment]):
        print("Not enough data yet for meaningful reflection. Keep building memories.")


def _cmd_forget() -> None:
    """Delete a memory by ID."""
    args = sys.argv[2:]
    if not args:
        print("Usage: brainvault forget <id>")
        sys.exit(1)

    memory_id = args[0]

    from brainvault import db

    db.init_db()
    deleted = db.delete_memory(memory_id)
    if deleted:
        print(f"Memory {memory_id} deleted.")
    else:
        print(f"Memory {memory_id} not found.")
        sys.exit(1)


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

"""
brainvault/cli.py — Command-line interface for brainvault.

Commands:
    brainvault install    — set up MCP server + Stop hook in Claude Code
    brainvault bootstrap  — seed memory from existing Claude Code session history
    brainvault search     — search stored memories
    brainvault stats      — show memory statistics
"""

import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        _print_usage()
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "install":
        _cmd_install()

    elif cmd == "uninstall":
        _cmd_uninstall()

    elif cmd == "doctor":
        _cmd_doctor()

    elif cmd == "export":
        _cmd_export()

    elif cmd == "import":
        _cmd_import()

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

    elif cmd == "sessions":
        _cmd_sessions()

    elif cmd == "activity":
        _cmd_activity()

    else:
        print(f"Unknown command: {cmd}")
        _print_usage()
        sys.exit(1)


def _print_usage() -> None:
    print("Usage: brainvault <command>")
    print()
    print("Commands:")
    print("  install    Detect coding agents, checklist (TTY) or typed selection, then MCP + hooks")
    print("  uninstall  Remove hooks + MCP entry from Claude Code (--purge also deletes DB)")
    print("  doctor     Diagnose install health: hooks, MCP, DB, optional deps")
    print("  export     Export memories + projects as JSON or Markdown")
    print("  import     Import a previously-exported JSON vault (merge by default)")
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
    print("  sessions   List recent agent sessions and their activity (Claude Code + Cursor)")
    print("  activity   Show full event timeline for a specific session")


def _parse_agents_flag(args: list[str]) -> list[str] | None:
    """
    Pull --agent / --agents off argv. Accepts repeated flags or comma-separated values.

    Returns None when the flag is absent (meaning: auto-detect installed hosts).
    """
    agents: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--agent", "--agents") and i + 1 < len(args):
            for piece in args[i + 1].split(","):
                piece = piece.strip()
                if piece:
                    agents.append(piece)
            i += 2
        else:
            i += 1
    return agents or None


def _tty_agent_checklist_ok() -> bool:
    """Use space/arrow checklist when stdin+stdout are TTYs (not piped / CI)."""
    if os.environ.get("BRAINVAULT_USE_LINE_AGENT_PICK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, AttributeError):
        return False


def _pick_agents_line_mode(detected: list, not_detected: list) -> list[str] | None:
    """Original comma-separated / numbered prompt (used when not a TTY or checklist fails)."""
    print("Detected coding agents:\n")
    for i, a in enumerate(detected, 1):
        print(f"  [{i}] {a.display_name}  ({a.name})")
    if not_detected:
        print()
        print("Not detected (can still force with --agent):")
        for a in not_detected:
            print(f"      {a.display_name}  ({a.name})")

    print()
    print("Which agents should brainvault install for?")
    print("  Enter numbers separated by commas, 'all', or press Enter for all.")
    print("  Example: 1,2   or   all   or   1")
    print()

    try:
        raw = input("  Your choice: ").strip()
    except EOFError:
        raw = ""

    if not raw or raw.lower() in ("a", "all"):
        return None  # install for everything detected

    if raw.lower() in ("n", "none", "0", "q", "quit"):
        print("\n  Aborted.")
        return []

    chosen: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(detected):
                chosen.append(detected[idx - 1].name)
            else:
                print(f"  ! '{token}' is out of range — ignored")
        else:
            match = next(
                (a for a in detected if a.name == token or a.display_name.lower() == token.lower()),
                None,
            )
            if match:
                chosen.append(match.name)
            else:
                print(f"  ! '{token}' not recognised — ignored")

    if not chosen:
        print("\n  No valid selection — aborted.")
        return []

    return list(dict.fromkeys(chosen))


def _pick_agents_interactive(skip_prompt: bool = False) -> list[str] | None:
    """
    Detect all known coding agents and let the user choose which ones to install for.

    Returns:
        None  — install for all detected (no prompt shown, or user chose "all")
        []    — user cancelled / nothing to install
        [...]  — explicit list of adapter names the user selected

    skip_prompt=True bypasses the selection (--yes / -y flag or non-interactive stdin).
    """
    from brainvault.adapters import ALL_ADAPTERS, all_adapters

    all_known = all_adapters()
    detected = [a for a in all_known if a.is_installed()]
    not_detected = [a for a in all_known if not a.is_installed()]

    if not detected:
        print("  No supported coding agents detected on this machine.")
        print("  Supported: " + ", ".join(cls.display_name for cls in ALL_ADAPTERS))
        print("\n  Install Claude Code or Cursor first, then re-run `brainvault install`.")
        return []

    if len(detected) == 1 or skip_prompt:
        # Single agent or non-interactive — no selection needed.
        return None  # caller treats None as "use installed_adapters()"

    if _tty_agent_checklist_ok():
        try:
            from brainvault.agent_picker import pick_agents_checklist

            if not_detected:
                print("Detected coding agents (installed):\n")
                print("Not installed here (use --agent to force):")
                for a in not_detected:
                    print(f"    · {a.display_name}  ({a.name})")
                print()
            return pick_agents_checklist(detected)
        except (OSError, AttributeError, ImportError, ValueError, RuntimeError, TypeError):
            # TTY checklist failed (e.g. termios on a non-terminal fd) — fall back to line mode.
            pass

    return _pick_agents_line_mode(detected, not_detected)


def _cmd_install() -> None:
    """Detect coding agents, optionally prompt for selection, then install."""
    from brainvault.installer import install

    args = sys.argv[2:]
    explicit_agents = _parse_agents_flag(args)
    yes = "--yes" in args or "-y" in args

    if explicit_agents is not None:
        # --agent flag given — skip interactive picker entirely.
        install(agents=explicit_agents)
        return

    selected = _pick_agents_interactive(skip_prompt=yes)
    if selected is not None and len(selected) == 0:
        # User cancelled or nothing to install.
        sys.exit(0)

    install(agents=selected)


def _cmd_uninstall() -> None:
    """Reverse install — strip hooks, MCP entry, and managed instruction blocks."""
    from brainvault.installer import uninstall

    args = sys.argv[2:]
    purge = "--purge" in args
    yes = "--yes" in args or "-y" in args
    agents = _parse_agents_flag(args)

    if purge and not yes:
        try:
            confirm = (
                input(
                    "This will delete ~/.brainvault/ including memory.db. Type 'yes' to continue: "
                )
                .strip()
                .lower()
            )
        except EOFError:
            confirm = ""
        if confirm != "yes":
            print("Aborted.")
            sys.exit(1)

    uninstall(purge=purge, agents=agents)


def _cmd_doctor() -> None:
    """
    Diagnose install health. Reports pass/fail per check and exits non-zero on failures.

    Core checks: DB path + integrity + FTS5, MCP module importable, git, semantic extras.
    Per-adapter checks: each installed adapter contributes its own health_checks().
    """
    import shutil
    import sqlite3
    import subprocess
    import sys as _sys
    from pathlib import Path

    from brainvault import db
    from brainvault.adapters import all_adapters

    print("Brainvault doctor\n")

    results: list[tuple[str, bool, str]] = []  # (label, ok, detail)

    def check(label: str, ok: bool, detail: str = "") -> None:
        results.append((label, ok, detail))

    # 1. DB path + integrity + FTS5
    db_path = db.get_db_path()
    if not db_path.exists():
        check("Database file", False, f"not found at {db_path} — run 'brainvault install'")
    else:
        try:
            with sqlite3.connect(db_path) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                check(
                    "Database integrity",
                    integrity == "ok",
                    f"{db_path} — {integrity}",
                )
                try:
                    conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()
                    check("FTS5 virtual table", True, "queryable")
                except sqlite3.OperationalError as e:
                    check("FTS5 virtual table", False, str(e))
        except sqlite3.DatabaseError as e:
            check("Database integrity", False, str(e))

    # 2. Per-adapter health checks
    any_installed = False
    for adapter in all_adapters():
        if not adapter.is_installed():
            continue
        any_installed = True
        for row in adapter.health_checks():
            results.append(row)

    if not any_installed:
        check(
            "Coding agent detected",
            False,
            "no supported agent (Claude Code / Cursor) found — install one and re-run",
        )

    # 3. MCP module importable in the current interpreter
    exe_to_test = _sys.executable
    if Path(exe_to_test).is_file():
        try:
            r = subprocess.run(
                [exe_to_test, "-c", "import brainvault.mcp_server"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode == 0:
                check("brainvault.mcp_server importable", True, exe_to_test)
            else:
                check(
                    "brainvault.mcp_server importable",
                    False,
                    (r.stderr.strip().splitlines() or [""])[-1],
                )
        except Exception as e:
            check("brainvault.mcp_server importable", False, str(e))

    # 4. Optional semantic stack
    try:
        from brainvault import embeddings as emb

        if emb._is_available():
            check("Semantic extras", True, "fastembed + sqlite-vec available (optional)")
        else:
            check("Semantic extras", True, "not installed (optional)")
    except Exception as e:
        check("Semantic extras", True, f"unavailable: {e} (optional)")

    # 5. git available — needed for git-scan + code-scan
    if shutil.which("git"):
        check("git on PATH", True, shutil.which("git") or "")
    else:
        check("git on PATH", False, "git not found — git-scan + index-repo will be unavailable")

    # Render + exit code
    failed = 0
    for label, ok, detail in results:
        mark = "✓" if ok else "✗"
        line = f"  {mark} {label}"
        if detail:
            line += f" — {detail}"
        print(line)
        if not ok:
            failed += 1

    print()
    if failed:
        print(f"{failed} check(s) failed. Run 'brainvault install' to repair, or open an issue.")
        sys.exit(1)
    else:
        print("All checks passed.")


# ---------------------------------------------------------------------------
# export / import — JSON + Markdown round-trip of the vault
# ---------------------------------------------------------------------------

EXPORT_SCHEMA_VERSION = 1


def _cmd_export() -> None:
    """Dump memories + projects to JSON (default) or Markdown."""
    import datetime
    import json as _json
    from pathlib import Path

    from brainvault import db

    args = sys.argv[2:]
    output: str | None = None
    fmt = "json"
    project: str | None = None
    include_events = False

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--output", "-o") and i + 1 < len(args):
            output = args[i + 1]
            i += 2
        elif a == "--format" and i + 1 < len(args):
            fmt = args[i + 1].lower()
            i += 2
        elif a == "--project" and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        elif a == "--include-events":
            include_events = True
            i += 1
        else:
            print(f"Unknown argument: {a}")
            print(
                "Usage: brainvault export [--output <path>] [--format json|md] "
                "[--project <name>] [--include-events]"
            )
            sys.exit(1)

    if fmt not in ("json", "md", "markdown"):
        print(f"Error: unsupported format '{fmt}'. Use 'json' or 'md'.")
        sys.exit(1)
    if fmt == "markdown":
        fmt = "md"

    db.init_db()

    with db.get_connection() as conn:
        if project:
            mem_rows = conn.execute(
                "SELECT * FROM memories WHERE project = ? ORDER BY created_at",
                (project,),
            ).fetchall()
            proj_rows = conn.execute("SELECT * FROM projects WHERE name = ?", (project,)).fetchall()
        else:
            mem_rows = conn.execute("SELECT * FROM memories ORDER BY created_at").fetchall()
            proj_rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()

        memories = [dict(r) for r in mem_rows]
        projects = [dict(r) for r in proj_rows]

        events = []
        if include_events:
            if project:
                ev_rows = conn.execute(
                    "SELECT * FROM session_events WHERE project = ? ORDER BY timestamp",
                    (project,),
                ).fetchall()
            else:
                ev_rows = conn.execute("SELECT * FROM session_events ORDER BY timestamp").fetchall()
            events = [dict(r) for r in ev_rows]

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": now,
        "project_filter": project,
        "projects": projects,
        "memories": memories,
        "events": events,
    }

    if fmt == "json":
        default_name = f"brainvault-export-{datetime.date.today().isoformat()}.json"
        out_path = Path(output) if output else Path.cwd() / default_name
        out_path.write_text(_json.dumps(payload, indent=2, default=str))
        print(f"Exported {len(memories)} memories and {len(projects)} projects → {out_path}")
        if include_events:
            print(f"  plus {len(events)} session events")
        return

    # Markdown rendering
    default_name = f"brainvault-export-{datetime.date.today().isoformat()}.md"
    out_path = Path(output) if output else Path.cwd() / default_name
    lines: list[str] = []
    lines.append("# Brainvault Export")
    lines.append("")
    lines.append(f"_Exported {now}_")
    lines.append("")
    lines.append(f"- **Memories**: {len(memories)}")
    lines.append(f"- **Projects**: {len(projects)}")
    if include_events:
        lines.append(f"- **Session events**: {len(events)}")
    lines.append("")

    if projects:
        lines.append("## Projects")
        lines.append("")
        for p in projects:
            stack = p.get("stack") or "[]"
            try:
                stack = (
                    ", ".join(_json.loads(stack)) if isinstance(stack, str) else ", ".join(stack)
                )
            except Exception:
                pass
            lines.append(f"### {p['name']}")
            if p.get("description"):
                lines.append(p["description"])
            lines.append(f"- Stack: {stack}")
            lines.append(f"- Status: {p.get('status', 'active')}")
            if p.get("notes"):
                lines.append(f"- Notes: {p['notes']}")
            lines.append("")

    # Group memories by project
    grouped: dict[str, list[dict]] = {}
    for m in memories:
        grouped.setdefault(m.get("project") or "global", []).append(m)

    for proj_name in sorted(grouped):
        lines.append(f"## Memories — {proj_name}")
        lines.append("")
        for m in grouped[proj_name]:
            created = (m.get("created_at") or "")[:10]
            mtype = m.get("memory_type", "note")
            source = m.get("source", "explicit")
            lines.append(f"### [{mtype}] {created} _(source: {source})_")
            content = (m.get("content") or "").strip()
            lines.append(content)
            if m.get("outcome"):
                sentiment = m.get("outcome_sentiment") or "?"
                lines.append("")
                lines.append(f"**Outcome ({sentiment})**: {m['outcome']}")
            lines.append("")
            lines.append(f"`id: {m['id']}`")
            lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"Exported {len(memories)} memories and {len(projects)} projects → {out_path}")


def _cmd_import() -> None:
    """Load a JSON export. Memories with colliding IDs are skipped unless --replace is passed."""
    import json as _json
    from pathlib import Path

    from brainvault import db

    args = sys.argv[2:]
    path_str: str | None = None
    replace = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--replace":
            replace = True
            i += 1
        elif not a.startswith("--"):
            if path_str is None:
                path_str = a
            i += 1
        else:
            print(f"Unknown argument: {a}")
            print("Usage: brainvault import <path.json> [--replace]")
            sys.exit(1)

    if not path_str:
        print("Usage: brainvault import <path.json> [--replace]")
        sys.exit(1)

    in_path = Path(path_str).expanduser()
    if not in_path.is_file():
        print(f"Error: {in_path} does not exist")
        sys.exit(1)

    try:
        payload = _json.loads(in_path.read_text())
    except _json.JSONDecodeError as e:
        print(f"Error: {in_path} is not valid JSON ({e.msg} at line {e.lineno})")
        sys.exit(1)

    if not isinstance(payload, dict) or "memories" not in payload:
        print("Error: export payload missing 'memories' key — wrong file?")
        sys.exit(1)

    schema_v = payload.get("schema_version")
    if schema_v and schema_v > EXPORT_SCHEMA_VERSION:
        print(
            f"Error: export schema_version={schema_v} is newer than this "
            f"brainvault (supports up to {EXPORT_SCHEMA_VERSION}). Upgrade and retry."
        )
        sys.exit(1)

    db.init_db()

    mem_imported = 0
    mem_skipped = 0
    mem_replaced = 0
    proj_imported = 0

    with db.get_connection() as conn:
        # Projects — upsert by name (ON CONFLICT DO UPDATE matches save_project).
        for p in payload.get("projects", []):
            try:
                stack = p.get("stack", "[]")
                if isinstance(stack, list):
                    stack = _json.dumps(stack)
                conn.execute(
                    """
                    INSERT INTO projects (name, description, stack, status, notes)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        description = excluded.description,
                        stack = excluded.stack,
                        status = excluded.status,
                        notes = excluded.notes,
                        updated_at = datetime('now')
                    """,
                    (
                        p["name"],
                        p.get("description", ""),
                        stack,
                        p.get("status", "active"),
                        p.get("notes", ""),
                    ),
                )
                proj_imported += 1
            except Exception as e:
                print(f"  (skipped project {p.get('name')}: {e})")

        for m in payload.get("memories", []):
            mid = m.get("id")
            if not mid:
                mem_skipped += 1
                continue
            existing = conn.execute("SELECT 1 FROM memories WHERE id = ?", (mid,)).fetchone()
            if existing and not replace:
                mem_skipped += 1
                continue

            keywords = m.get("keywords", "[]")
            if isinstance(keywords, list):
                keywords = _json.dumps(keywords)

            if existing and replace:
                conn.execute(
                    """
                    UPDATE memories SET content=?, memory_type=?, project=?, keywords=?,
                        source=?, outcome=?, outcome_sentiment=?
                    WHERE id=?
                    """,
                    (
                        m.get("content", ""),
                        m.get("memory_type", "note"),
                        m.get("project"),
                        keywords,
                        m.get("source", "explicit"),
                        m.get("outcome"),
                        m.get("outcome_sentiment"),
                        mid,
                    ),
                )
                mem_replaced += 1
            else:
                conn.execute(
                    """
                    INSERT INTO memories
                        (id, content, memory_type, project, keywords, source,
                         created_at, outcome, outcome_sentiment)
                    VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?, ?)
                    """,
                    (
                        mid,
                        m.get("content", ""),
                        m.get("memory_type", "note"),
                        m.get("project"),
                        keywords,
                        m.get("source", "explicit"),
                        m.get("created_at"),
                        m.get("outcome"),
                        m.get("outcome_sentiment"),
                    ),
                )
                mem_imported += 1

    print(
        f"Imported {mem_imported} new memories, "
        f"replaced {mem_replaced}, "
        f"skipped {mem_skipped} (existing). "
        f"Projects touched: {proj_imported}."
    )
    print("Run 'brainvault embed' to regenerate embeddings for imported rows.")


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
            source_agent=db.SYSTEM_SOURCE_AGENT,
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


def _cmd_sessions() -> None:
    """List recent sessions (session_events) and their tool activity."""
    from brainvault import db

    args = sys.argv[2:]
    project = None
    days = 7

    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        elif args[i] == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                print(f"Error: --days must be an integer, got '{args[i + 1]}'")
                sys.exit(1)
            i += 2
        else:
            i += 1

    db.init_db()
    data = db.get_recent_activity(project=project, days=days)
    sessions = data.get("sessions", [])
    total = data.get("total_events", 0)

    scope = f" [{project}]" if project else ""
    print(f"Recent sessions{scope} — last {days} days\n")

    if not sessions:
        print("  No sessions recorded.")
        return

    print(f"  {total} events across {len(sessions)} session(s)\n")
    for s in sessions:
        proj_label = f"[{s['project']}] " if s.get("project") else ""
        first = (s.get("first_event") or "")[:16]
        last = (s.get("last_event") or "")[:16]
        tools = ", ".join(s.get("tools", []))
        print(f"  {proj_label}{s['session_id'][:16]}…")
        print(f"    {first} → {last}  |  {s['event_count']} events  |  {tools}")
        print(f"    brainvault activity {s['session_id']}")
        print()


def _cmd_activity() -> None:
    """Show the full event timeline for a session ID."""
    from brainvault import db

    args = sys.argv[2:]
    if not args or args[0].startswith("--"):
        print("Usage: brainvault activity <session-id>")
        sys.exit(1)

    session_id = args[0]

    db.init_db()
    events = db.get_session_timeline(session_id)

    if not events:
        print(f"No events found for session '{session_id}'.")
        return

    print(f"Session timeline: {session_id[:16]}…  ({len(events)} events)\n")
    for ev in events:
        ts = (ev.get("timestamp") or "")[:16]
        tool = ev["tool_name"]
        summary = ev.get("input_summary", "")
        out = ev.get("output_summary", "")
        out_str = f"  → {out}" if out else ""
        print(f"  {ts}  {tool:<16}  {summary}{out_str}")

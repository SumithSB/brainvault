"""
brainvault/mcp_server.py — MCP server exposing memory tools to coding agents.

Runs as a stdio MCP server (``python -m brainvault.mcp_server``). The connecting
host (e.g. Claude Code, Cursor) sets ``BRAINVAULT_SOURCE_AGENT`` when present.
Optional ``BRAINVAULT_MCP_TERSE=1`` shortens tool return strings to save tokens.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

from brainvault import db
from brainvault.mcp_terse import (
    VERBOSE_MEMORY_PREVIEW_CHARS,
    effective_search_max_chars,
    mcp_terse_enabled,
)

mcp = FastMCP("brainvault")

_DEFAULT_SEARCH_MEMORY_MAX_CHARS = VERBOSE_MEMORY_PREVIEW_CHARS


def _preview_memory_content(text: str, memory_id: str, max_chars: int) -> str:
    """Truncate memory text for MCP tool output (token control); full text stays in DB."""
    if len(text) <= max_chars:
        return text
    if mcp_terse_enabled():
        return text[:max_chars].rstrip() + f"…id={memory_id[:8]}"
    return text[:max_chars].rstrip() + f"… (id: {memory_id})"


def _reflect_output_terse(data: dict) -> str:
    """Compact reflection for BRAINVAULT_MCP_TERSE=1."""
    parts: list[str] = []
    open_decisions = data["open_decisions"]
    if open_decisions:
        parts.append(f"OD:{len(open_decisions)}")
        for d in open_decisions[:25]:
            proj = d["project"] or "."
            c = d["content"].replace("\n", " ")[:100]
            parts.append(f"{d['id'][:8]}|[{proj}]{c}")
    patterns = data["cross_project_patterns"]
    if patterns:
        parts.append(f"PAT:{len(patterns)}")
        for kw, projs in patterns[:20]:
            parts.append(f"{kw}→{','.join(projs[:6])}")
    stale = data["stale_projects"]
    if stale:
        parts.append(f"ST:{len(stale)}")
        for p in stale[:15]:
            last = (p.get("last_active") or p.get("updated_at") or "")[:10]
            parts.append(f"{p['name']}:{last}")
    hot = data["hot_memories"]
    if hot:
        parts.append(f"HOT:{len(hot)}")
        for m in hot[:12]:
            proj = m["project"] or "."
            c = m["content"].replace("\n", " ")[:80]
            parts.append(f"{m['memory_type']}|[{proj}]×{m['access_count']} {c}")
    sentiment = data.get("outcome_sentiment_summary", {})
    if sentiment:
        parts.append("OUT:" + ",".join(f"{k}={v}" for k, v in sentiment.items() if v))
    if not parts:
        return "refl ∅"
    return "\n".join(parts)


@mcp.tool()
def get_my_context() -> str:
    """
    Load your personal context — who you are, your active projects, and global patterns.
    Call this at the start of every session to get up to speed immediately.
    """
    db.init_db()
    with db.get_connection() as conn:
        profiles = conn.execute(
            "SELECT * FROM memories WHERE memory_type = 'profile' AND project IS NULL ORDER BY created_at DESC"
        ).fetchall()
        profiles = [dict(p) for p in profiles]

    projects = db.list_projects(status="active")
    stats = db.get_stats()

    if not profiles and not projects:
        if mcp_terse_enabled():
            return "empty → save_memory(profile)|register_project|save_memory(decision)"
        return (
            "No context stored yet.\n"
            "To build your brainvault:\n"
            "- Describe yourself: call save_memory with type 'profile'\n"
            "- Register a project: call register_project\n"
            "- Save decisions as you make them: call save_memory with type 'decision'"
        )

    if mcp_terse_enabled():
        parts: list[str] = [
            f"T={stats['total_memories']} P={stats['total_projects']}",
        ]
        for p in profiles:
            parts.append(f"pf:{p['content'][:240]}{'…' if len(p['content']) > 240 else ''}")
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        for p in projects:
            stack = json.loads(p["stack"]) if isinstance(p["stack"], str) else p["stack"]
            stack_str = ",".join(stack) if stack else "—"
            last_active = p.get("last_active") or p.get("updated_at")
            stale = ""
            if last_active:
                try:
                    la = datetime.datetime.fromisoformat(last_active)
                    days_idle = (now - la).days
                    if days_idle >= 30:
                        stale = f"~{days_idle}d"
                except ValueError:
                    pass
            bit = f"{p['name']}|{stack_str}|{p['description'][:100]}{stale}"
            if p["notes"]:
                bit += f"|n:{p['notes'][:80]}"
            parts.append(bit)
        return "\n".join(parts)

    lines = ["# Your Brainvault Context\n"]

    if profiles:
        lines.append("## About You")
        for p in profiles:
            lines.append(f"- {p['content']}")
        lines.append("")

    if projects:
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        lines.append("## Active Projects")
        for p in projects:
            stack = json.loads(p["stack"]) if isinstance(p["stack"], str) else p["stack"]
            stack_str = ", ".join(stack) if stack else "not specified"
            # Staleness indicator
            last_active = p.get("last_active") or p.get("updated_at")
            stale_flag = ""
            if last_active:
                try:
                    la = datetime.datetime.fromisoformat(last_active)
                    days_idle = (now - la).days
                    if days_idle >= 30:
                        stale_flag = f" ⚠️ idle {days_idle}d"
                except ValueError:
                    pass
            lines.append(f"- **{p['name']}**{stale_flag} — {p['description']} (stack: {stack_str})")
            if p["notes"]:
                lines.append(f"  Notes: {p['notes']}")
        lines.append("")

    lines.append(
        f"## Stats\n{stats['total_memories']} memories stored across {stats['total_projects']} projects."
    )

    return "\n".join(lines)


@mcp.tool()
def search_memory(
    query: str, project: str = None, max_chars: int = _DEFAULT_SEARCH_MEMORY_MAX_CHARS
) -> str:
    """
    Search your memory for context relevant to a query.

    Call this when:
    - Starting non-trivial work where prior notes might exist (design, debugging, refactors)
    - User mentions a topic you might have decided before
    - User asks 'do you remember...' or 'we discussed...'
    - Before making a recommendation that could contradict stored decisions

    Args:
        query: What to search for (e.g. 'error handling convention', 'migration strategy')
        project: Optional project name to prioritise results from that project
        max_chars: Max characters per memory body in the response (default 400, or 200 when
            BRAINVAULT_MCP_TERSE=1); full text remains in DB
    """
    db.init_db()
    if not query.strip():
        return "need query" if mcp_terse_enabled() else "Please provide a search query."

    cap = effective_search_max_chars(max_chars, default_verbose=_DEFAULT_SEARCH_MEMORY_MAX_CHARS)
    results = db.search_memories(query, project=project, limit=5)

    if not results:
        return f"0 hits q={query!r}" if mcp_terse_enabled() else f'No relevant memory found for: "{query}"'

    if mcp_terse_enabled():
        out: list[str] = [f"n={len(results)} q={query!r}"]
        for i, m in enumerate(results, 1):
            proj_label = m["project"] or "."
            fts_hit = m.pop("_fts_rank", None) is not None
            vec_hit = m.pop("_vec_rank", None) is not None
            if vec_hit and fts_hit:
                rnk = "H"
            elif vec_hit:
                rnk = "V"
            else:
                rnk = "F"
            body = _preview_memory_content(m["content"], m["id"], cap)
            kw = json.loads(m["keywords"]) if isinstance(m["keywords"], str) else m["keywords"]
            kw_s = ",".join(kw[:5]) if kw else ""
            tail = f"|{kw_s}" if kw_s else ""
            out.append(f"{i}|{m['memory_type']}|{proj_label}|{rnk}|{body}|{m['id'][:8]}{tail}")
        return "\n".join(out)

    lines = [f'Found {len(results)} memories for "{query}":\n']
    for i, m in enumerate(results, 1):
        proj_label = m["project"] or "global"
        keywords = json.loads(m["keywords"]) if isinstance(m["keywords"], str) else m["keywords"]
        fts_hit = m.pop("_fts_rank", None) is not None
        vec_hit = m.pop("_vec_rank", None) is not None
        if vec_hit and fts_hit:
            tag = " [hybrid]"
        elif vec_hit:
            tag = " [semantic]"
        else:
            tag = ""
        lines.append(f"[{i}] {m['memory_type']} · project: {proj_label}{tag}")
        body = _preview_memory_content(m["content"], m["id"], cap)
        lines.append(f"    {body}")
        if keywords:
            lines.append(f"    Keywords: {', '.join(keywords[:6])}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def save_memory(
    content: str,
    memory_type: str,
    project: str = None,
    keywords: list[str] = None,
) -> str:
    """
    Save a memory to your brainvault.

    Call this when:
    - User says 'remember this', 'save this', 'note this'
    - User describes themselves, their preferences, or their working style (type: profile)
    - User makes an architectural decision with reasoning (type: decision)
    - User establishes a pattern or convention (type: pattern)
    - User describes a project (use register_project instead for full project context)

    Args:
        content: The memory to save — be specific and include the reasoning, not just the conclusion
        memory_type: One of: profile | decision | pattern | note
        project: Project name to scope this memory to (omit for global memories)
        keywords: Optional list of keywords. Auto-extracted if not provided.
    """
    db.init_db()
    if memory_type not in db.VALID_MEMORY_TYPES:
        if mcp_terse_enabled():
            return f"bad type {memory_type!r}"
        return f"Invalid memory_type '{memory_type}'. Must be one of: {', '.join(sorted(db.VALID_MEMORY_TYPES))}"

    source_agent = (os.environ.get("BRAINVAULT_SOURCE_AGENT") or "claude_code").strip()
    if source_agent not in db.VALID_SOURCE_AGENTS:
        source_agent = "claude_code"

    memory_id = db.save_memory(
        content=content,
        memory_type=memory_type,
        project=project,
        keywords=keywords,
        source="agent",
        source_agent=source_agent,
    )
    if mcp_terse_enabled():
        sc = f"p={project}" if project else "g"
        return f"ok {memory_id[:8]} {memory_type} {sc}"
    scope = f"project: {project}" if project else "global"
    return f"Saved. Memory ID: {memory_id} ({memory_type} · {scope})"


@mcp.tool()
def register_project(
    name: str,
    description: str,
    stack: list[str],
    notes: str = "",
) -> str:
    """
    Register or update a project in your brainvault.

    Call this when the user describes a project at the start of a session or mentions
    working on something new.

    Example trigger: 'I'm working on the billing service — Go microservice, talks to Postgres'

    Args:
        name: Short project identifier (e.g. 'billing', 'mobile-app')
        description: What the project does
        stack: Technologies used (e.g. ['TypeScript', 'React', 'Postgres'])
        notes: Any additional context worth remembering
    """
    db.init_db()
    if not name.strip():
        return "Project name cannot be empty."

    db.save_project(name=name, description=description, stack=stack, notes=notes)
    stack_str = ", ".join(stack) if stack else "not specified"
    if mcp_terse_enabled():
        return f"ok proj={name} {stack_str}"
    return f"Project '{name}' saved. Stack: {stack_str}."


@mcp.tool()
def get_project(name: str, limit: int = 20) -> str:
    """
    Get everything stored about a specific project — description, stack, notes, and memories.

    Call this when the user mentions a project by name at the start of a session.

    Args:
        name: The project name (e.g. 'billing')
        limit: Max memories listed (newest first); use search_memory to drill into large projects
    """
    db.init_db()
    project = db.get_project(name)
    memories = db.get_project_memories(name)

    if not project and not memories:
        return f"missing {name!r}" if mcp_terse_enabled() else f"Project '{name}' not found. Use register_project to add it."

    if mcp_terse_enabled():
        chunks: list[str] = []
        if project:
            stack = (
                json.loads(project["stack"]) if isinstance(project["stack"], str) else project["stack"]
            )
            stk = ",".join(stack) if stack else "—"
            chunks.append(f"{project['name']}|{stk}|{project['description'][:120]}")
            if project["notes"]:
                chunks.append(f"n:{project['notes'][:100]}")
        if memories:
            total = len(memories)
            shown = memories[:limit]
            chunks.append(f"m:{total}/{limit}")
            for m in shown:
                c = m["content"].replace("\n", " ")[:160]
                chunks.append(f"[{m['memory_type']}] {c}{'…' if len(m['content']) > 160 else ''}")
        return "\n".join(chunks)

    lines = []

    if project:
        stack = (
            json.loads(project["stack"]) if isinstance(project["stack"], str) else project["stack"]
        )
        lines.append(f"# Project: {project['name']}")
        lines.append(f"**Description:** {project['description']}")
        lines.append(f"**Stack:** {', '.join(stack) if stack else 'not specified'}")
        lines.append(f"**Status:** {project['status']}")
        if project["notes"]:
            lines.append(f"**Notes:** {project['notes']}")
        lines.append("")

    if memories:
        total = len(memories)
        shown = memories[:limit]
        lines.append(f"## Memories ({total})")
        for m in shown:
            lines.append(f"- [{m['memory_type']}] {m['content']}")
        if total > limit:
            more = total - limit
            lines.append(
                f"… {more} more — call `search_memory` with a topic and project={name!r} to drill in."
            )
    else:
        lines.append("No memories stored for this project yet.")

    return "\n".join(lines)


@mcp.tool()
def record_outcome(memory_id: str, outcome: str, sentiment: str = None) -> str:
    """
    Record the real-world outcome of a past decision.

    Call this when:
    - A feature built on a past decision shipped and worked (or failed)
    - A user reports that a previous architectural choice caused issues
    - A decision has been revisited and the result is now known

    This closes the feedback loop so brainvault can surface what actually worked
    vs. what was only hypothesised. Crucial for the 'virtual brain' gap analysis.

    Args:
        memory_id: UUID of the decision memory (from search results or save confirmations)
        outcome: What actually happened — be specific (e.g. 'Held up under load in production'
                 or 'Had to roll back: edge cases in mobile clients')
        sentiment: One of 'positive', 'negative', 'mixed' — overall verdict on how the decision played out
    """
    db.init_db()
    if not outcome.strip():
        return "Outcome cannot be empty."
    updated = db.record_outcome(memory_id, outcome, sentiment=sentiment)
    if updated:
        if mcp_terse_enabled():
            s = f" {sentiment}" if sentiment else ""
            return f"ok outcome {memory_id[:8]}{s}"
        sentiment_note = f" (sentiment: {sentiment})" if sentiment else ""
        return f"Outcome recorded for decision {memory_id}{sentiment_note}."
    if mcp_terse_enabled():
        return f"fail {memory_id[:8]} not decision"
    return (
        f"Memory {memory_id} not found or is not type 'decision'. "
        "Use search_memory to find the correct ID."
    )


@mcp.tool()
def reflect() -> str:
    """
    Surface cross-project patterns, open decisions, and knowledge gaps.

    Call this when:
    - User asks 'what patterns do I repeat?', 'what are my gaps?', 'what decisions are unresolved?'
    - Starting a new architectural decision — check what was decided before across all projects
    - Periodic review: weekly/monthly to see what's drifting or unresolved

    Returns:
    - Open decisions that have no outcome recorded (potential forgotten follow-ups)
    - Cross-project patterns (topics that keep coming up across multiple projects)
    - Stale projects (no activity in 30+ days — may need a decision on whether to archive)
    - Hot memories (most frequently accessed — what sessions keep revisiting)
    """
    db.init_db()
    data = db.get_reflection_data()

    if mcp_terse_enabled():
        return _reflect_output_terse(data)

    lines = ["# Brainvault Reflection\n"]

    open_decisions = data["open_decisions"]
    if open_decisions:
        lines.append(f"## Open Decisions ({len(open_decisions)} unresolved)")
        lines.append("These decisions were made but have no recorded outcome yet:\n")
        for d in open_decisions:
            proj = d["project"] or "global"
            created = d["created_at"][:10] if d["created_at"] else "?"
            lines.append(f"- [{proj}] {d['content'][:120]}{'…' if len(d['content']) > 120 else ''}")
            lines.append(f"  ID: {d['id']} · decided: {created}")
        lines.append("")
    else:
        lines.append("## Open Decisions\nAll decisions have recorded outcomes. ✓\n")

    patterns = data["cross_project_patterns"]
    if patterns:
        lines.append("## Cross-Project Patterns")
        lines.append("Topics that appear across multiple projects — your recurring concerns:\n")
        for kw, projs in patterns:
            lines.append(f"- **{kw}** → {', '.join(projs)}")
        lines.append("")

    stale = data["stale_projects"]
    if stale:
        lines.append("## Stale Projects")
        lines.append("Active projects with no memory activity in 30+ days:\n")
        for p in stale:
            last = (p.get("last_active") or p.get("updated_at") or "")[:10]
            lines.append(f"- **{p['name']}** — last active: {last}")
        lines.append("")

    hot = data["hot_memories"]
    if hot:
        lines.append("## Most Accessed Memories")
        lines.append("Often consulted — typically high-value context:\n")
        for m in hot:
            proj = m["project"] or "global"
            lines.append(
                f"- [{m['memory_type']} · {proj}] {m['content'][:100]}{'…' if len(m['content']) > 100 else ''}"
            )
            lines.append(f"  Accessed {m['access_count']} times")
        lines.append("")

    sentiment = data.get("outcome_sentiment_summary", {})
    if sentiment:
        total_outcomes = sum(sentiment.values())
        lines.append("## Decision Outcomes")
        lines.append(f"{total_outcomes} decisions have recorded outcomes:\n")
        for label in ("positive", "negative", "mixed", "unrated"):
            count = sentiment.get(label, 0)
            if count:
                bar = "█" * min(count, 20)
                lines.append(f"  {label:<10} {bar} {count}")
        lines.append("")

    if not any([open_decisions, patterns, stale, hot, sentiment]):
        lines.append("Not enough data yet for meaningful reflection. Keep building memories.")

    return "\n".join(lines)


@mcp.tool()
def get_code_context(
    project: str,
    query: str,
    include_files: bool = True,
) -> str:
    """
    Get targeted code context before starting feature work on a project.

    Call this INSTEAD OF reading many files when:
    - Starting a non-trivial feature or cross-cutting change
    - About to touch a module not seen in this session
    - User asks "where should I add X?" or "what files touch Y?"
    - Making a change that likely spans 2+ files

    Skip for: single-file edits you already know, cosmetic changes.

    Args:
        project:       Project name (must match a registered project).
        query:         What you're about to work on — specific beats vague (e.g. "add retry to
                       checkout client" beats "checkout").
        include_files: Include ranked file list with co-change data (default True).
                       Set False to get only relevant memories.

    Returns structured context:
    - Relevant past decisions and patterns
    - Ranked files to read, with reason and co-change partners
    - "Also edit" hints from co-change data — files that historically change together
    """
    db.init_db()
    if not project.strip():
        return "Error: project name cannot be empty."
    if not query.strip():
        return "Error: query cannot be empty."

    data = db.get_code_context_data(project=project, query=query, limit=5)

    if mcp_terse_enabled():
        chunks: list[str] = [f"ctx {project!r} q={query!r}"]
        for m in data.get("memories", []):
            tag = "g" if m.get("source") == "git" else ""
            c = m["content"].replace("\n", " ")[:140]
            chunks.append(f"{m['memory_type']}{tag}|{c}")
        if include_files:
            for f in data.get("ranked_files", [])[:12]:
                line = f"{f['file_path']}|{f['language']}|{f['reason'][:80]}"
                if f.get("cochange_partners"):
                    line += f"|+{','.join(f['cochange_partners'][:3])}"
                chunks.append(line)
        return "\n".join(chunks)

    lines = [f"# Code Context: {project} — {query}\n"]

    memories = data.get("memories", [])
    if memories:
        lines.append("## Relevant Decisions & Patterns\n")
        for m in memories:
            tag = " [git]" if m.get("source") == "git" else ""
            content = m["content"][:200] + ("…" if len(m["content"]) > 200 else "")
            lines.append(f"- [{m['memory_type']}{tag}] {content}")
        lines.append("")
    else:
        lines.append("## Relevant Decisions & Patterns\nNone found for this query.\n")

    if include_files:
        ranked = data.get("ranked_files", [])
        if ranked:
            lines.append("## Files to Read (ranked by relevance)\n")
            for i, f in enumerate(ranked, 1):
                lines.append(f"{i}. `{f['file_path']}` ({f['language']})")
                lines.append(f"   Why: {f['reason']}")
                if f.get("cochange_partners"):
                    partners = ", ".join(f"`{p}`" for p in f["cochange_partners"])
                    lines.append(f"   Also edit: {partners}")
                lines.append("")
        else:
            if not db.is_repo_indexed(db.get_project_repo_path(project) or ""):
                lines.append(
                    "## Files to Read\n"
                    f"No structural index found. Run: `brainvault index-repo <path> --project {project}`\n"
                )
            else:
                lines.append("## Files to Read\nNo matching files for this query.\n")

    return "\n".join(lines)


@mcp.tool()
def update_memory(
    memory_id: str,
    content: str = None,
    memory_type: str = None,
    project: str = None,
) -> str:
    """
    Edit an existing memory in place.

    Call this when:
    - A memory is no longer accurate and needs correction
    - User says 'update that memory', 'that's changed', 'actually it's now...'
    - A pattern or decision has evolved and the old wording is misleading

    Args:
        memory_id: UUID of the memory to update (from search results or save confirmations)
        content: New content to replace the existing text (omit to keep unchanged)
        memory_type: New type — one of: profile | decision | pattern | note (omit to keep unchanged)
        project: New project scope (omit to keep unchanged)
    """
    db.init_db()
    if content is None and memory_type is None and project is None:
        return "upd noop" if mcp_terse_enabled() else "No changes requested. Provide at least one of: content, memory_type, project."

    if memory_type and memory_type not in db.VALID_MEMORY_TYPES:
        if mcp_terse_enabled():
            return f"bad type {memory_type!r}"
        return f"Invalid memory_type '{memory_type}'. Must be one of: {', '.join(sorted(db.VALID_MEMORY_TYPES))}"

    updated = db.update_memory(
        memory_id,
        content=content,
        memory_type=memory_type,
        project=project,
    )
    if updated:
        if mcp_terse_enabled():
            parts = [p for p in [content and "c", memory_type and "t", project and "p"] if p]
            return f"upd {memory_id[:8]} {'+'.join(parts) or 'ok'}"
        parts = [
            f for f, v in [("content", content), ("type", memory_type), ("project", project)] if v
        ]
        return f"Memory {memory_id} updated ({', '.join(parts)})."
    return f"!mem {memory_id[:8]}" if mcp_terse_enabled() else f"Memory {memory_id} not found."


@mcp.tool()
def forget(memory_id: str = None, project: str = None) -> str:
    """
    Delete a specific memory by ID, or all memories for a project.

    Call with memory_id when user says 'forget that', 'remove that memory', or 'that's no longer true'.
    Call with project when user says 'forget everything about <project>' or 'wipe <project> memories'.

    Args:
        memory_id: UUID of a single memory to delete (mutually exclusive with project)
        project:   Project name — deletes ALL memories for that project
    """
    db.init_db()
    if project and memory_id:
        return "Error: provide memory_id OR project, not both."
    if project:
        count = db.delete_project_memories(project)
        if count:
            return f"del proj:{project} ({count})" if mcp_terse_enabled() else f"Deleted {count} memories for project '{project}'."
        return f"!proj {project[:16]}" if mcp_terse_enabled() else f"No memories found for project '{project}'."
    if memory_id:
        deleted = db.delete_memory(memory_id)
        if deleted:
            return f"del {memory_id[:8]}" if mcp_terse_enabled() else f"Memory {memory_id} deleted."
        return f"!mem {memory_id[:8]}" if mcp_terse_enabled() else f"Memory {memory_id} not found."
    return "Error: provide memory_id or project."


@mcp.tool()
def get_recent_activity(
    project: str = None,
    days: int = 7,
) -> str:
    """
    Return a compact index of recent coding-agent tool activity across sessions
    (Claude Code, Cursor, etc. — see `source_agent` on stored rows).

    Surfaces what files were written/edited, what commands ran, and which sessions
    were most active — without loading the full event stream.

    Call this when:
    - User asks "what did I work on recently?"
    - You want context about recent changes before starting a task
    - You need to know which sessions touched a specific project

    Args:
        project: Filter to a specific project name (omit for all projects)
        days: How many days back to look (default: 7)
    """
    db.init_db()
    data = db.get_recent_activity(project=project, days=days)
    sessions = data.get("sessions", [])
    total = data.get("total_events", 0)

    if not sessions:
        scope = f" for project '{project}'" if project else ""
        if mcp_terse_enabled():
            return f"act ∅ {days}d{scope}"
        return f"No activity recorded in the last {days} days{scope}."

    if mcp_terse_enabled():
        bits = [f"act {days}d ev={total} s={len(sessions)}"]
        for s in sessions:
            sid = s["session_id"]
            proj_label = s.get("project") or "."
            tools = ",".join(s.get("tools", [])[:8])
            bits.append(f"{sid[:12]}|{proj_label}|{s['event_count']}|{tools}")
        return "\n".join(bits)

    lines = [f"# Recent Activity (last {days} days)\n"]
    lines.append(f"Total events: {total} across {len(sessions)} session(s)\n")

    for s in sessions:
        session_id = s["session_id"]
        proj_label = f"[{s['project']}] " if s.get("project") else ""
        first = (s.get("first_event") or "")[:16]
        last = (s.get("last_event") or "")[:16]
        tools = ", ".join(s.get("tools", []))
        lines.append(f"## {proj_label}Session `{session_id[:12]}…`")
        lines.append(f"Period: {first} → {last}  |  {s['event_count']} events  |  Tools: {tools}")
        lines.append(f"_For full timeline: call `get_session_timeline('{session_id}')`_\n")

    return "\n".join(lines)


@mcp.tool()
def get_session_timeline(session_id: str, limit: int = 50) -> str:
    """
    Return the ordered event timeline for a specific session (Claude Code, Cursor, etc.).

    Shows tool calls with compact summaries. By default returns the **most recent** events
    (last ``limit`` rows by insert order); older events are omitted with a footer.

    Call this when:
    - User asks "what did I do in that session?"
    - You need to reconstruct the sequence of changes in a past session
    - Debugging what happened during a specific working period

    Args:
        session_id: Session ID from get_recent_activity output
        limit: Max events to return (default 50); increase to see earlier events in long sessions
    """
    db.init_db()
    events_all = db.get_session_timeline(session_id)

    if not events_all:
        return f"tl - {session_id[:12]}" if mcp_terse_enabled() else f"No events found for session '{session_id}'."

    total = len(events_all)
    older_truncated = 0
    if total > limit:
        older_truncated = total - limit
        events = events_all[-limit:]
    else:
        events = events_all

    if mcp_terse_enabled():
        hdr = f"tl {session_id[:12]} n={total}"
        if older_truncated:
            hdr += f" +{older_truncated}trunc"
        bits = [hdr]
        for ev in events:
            ts = (ev.get("timestamp") or "")[:16]
            tool = ev["tool_name"]
            summary = (ev.get("input_summary", "") or "")[:120]
            out = (ev.get("output_summary", "") or "")[:60]
            bits.append(f"{ts}|{tool}|{summary}|{out}")
        return "\n".join(bits)

    lines = [f"# Session Timeline: `{session_id[:12]}…`\n"]
    lines.append(f"{total} events recorded\n")
    if older_truncated:
        lines.append(
            f"… {older_truncated} older event(s) truncated (default limit={limit}); "
            "pass a higher limit to see earlier events.\n"
        )

    for ev in events:
        ts = (ev.get("timestamp") or "")[:16]
        tool = ev["tool_name"]
        summary = ev.get("input_summary", "")
        out = ev.get("output_summary", "")
        out_str = f"  → {out}" if out else ""
        lines.append(f"`{ts}` **{tool}** {summary}{out_str}")

    return "\n".join(lines)


if __name__ == "__main__":
    db.init_db()
    mcp.run(transport="stdio")

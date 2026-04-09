"""
brainvault/mcp_server.py — MCP server exposing memory tools to Claude Code.
Runs as a stdio MCP server: python -m brainvault.mcp_server
"""

import json

from mcp.server.fastmcp import FastMCP

from brainvault import db

mcp = FastMCP("brainvault")


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
        return (
            "No context stored yet.\n"
            "To build your brainvault:\n"
            "- Describe yourself: call save_memory with type 'profile'\n"
            "- Register a project: call register_project\n"
            "- Save decisions as you make them: call save_memory with type 'decision'"
        )

    lines = ["# Your Brainvault Context\n"]

    if profiles:
        lines.append("## About You")
        for p in profiles:
            lines.append(f"- {p['content']}")
        lines.append("")

    if projects:
        import datetime

        now = datetime.datetime.utcnow()
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
def search_memory(query: str, project: str = None) -> str:
    """
    Search your memory for context relevant to a query.

    Call this when:
    - Starting work on a non-trivial feature (auth, DB design, API structure, deployment)
    - User mentions a topic you might have decided before
    - User asks 'do you remember...' or 'we discussed...'
    - Before making an architectural recommendation

    Args:
        query: What to search for (e.g. 'auth', 'database choice', 'rate limiting')
        project: Optional project name to prioritise results from that project
    """
    db.init_db()
    if not query.strip():
        return "Please provide a search query."

    results = db.search_memories(query, project=project, limit=5)

    if not results:
        return f'No relevant memory found for: "{query}"'

    lines = [f'Found {len(results)} memories for "{query}":\n']
    for i, m in enumerate(results, 1):
        proj_label = m["project"] or "global"
        keywords = json.loads(m["keywords"]) if isinstance(m["keywords"], str) else m["keywords"]
        lines.append(f"[{i}] {m['memory_type']} · project: {proj_label}")
        lines.append(f"    {m['content']}")
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
    valid_types = {"profile", "project", "decision", "pattern", "note"}
    if memory_type not in valid_types:
        return (
            f"Invalid memory_type '{memory_type}'. Must be one of: {', '.join(sorted(valid_types))}"
        )

    memory_id = db.save_memory(
        content=content,
        memory_type=memory_type,
        project=project,
        keywords=keywords,
        source="agent",
    )
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

    Example trigger: 'I'm working on pluto — FastAPI backend, PostgreSQL, handles ML jobs'

    Args:
        name: Short project identifier (e.g. 'pluto', 'ivy', 'studia')
        description: What the project does
        stack: Technologies used (e.g. ['FastAPI', 'PostgreSQL', 'Redis'])
        notes: Any additional context worth remembering
    """
    db.init_db()
    if not name.strip():
        return "Project name cannot be empty."

    db.save_project(name=name, description=description, stack=stack, notes=notes)
    stack_str = ", ".join(stack) if stack else "not specified"
    return f"Project '{name}' saved. Stack: {stack_str}."


@mcp.tool()
def get_project(name: str) -> str:
    """
    Get everything stored about a specific project — description, stack, notes, and all memories.

    Call this when the user mentions a project by name at the start of a session.

    Args:
        name: The project name (e.g. 'pluto')
    """
    db.init_db()
    project = db.get_project(name)
    memories = db.get_project_memories(name)

    if not project and not memories:
        return f"Project '{name}' not found. Use register_project to add it."

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
        lines.append(f"## Memories ({len(memories)})")
        for m in memories:
            lines.append(f"- [{m['memory_type']}] {m['content']}")
    else:
        lines.append("No memories stored for this project yet.")

    return "\n".join(lines)


@mcp.tool()
def record_outcome(memory_id: str, outcome: str) -> str:
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
        outcome: What actually happened — be specific (e.g. 'JWT worked well at scale,
                 no issues after 6 months' or 'caused auth bugs on mobile, switched to sessions')
    """
    db.init_db()
    if not outcome.strip():
        return "Outcome cannot be empty."
    updated = db.record_outcome(memory_id, outcome)
    if updated:
        return f"Outcome recorded for decision {memory_id}."
    return f"Decision {memory_id} not found. Only 'decision' type memories can have outcomes."


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
    - Hot memories (most frequently accessed — what Claude keeps needing to look up)
    """
    db.init_db()
    data = db.get_reflection_data()

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
        lines.append("What Claude keeps looking up — your high-value knowledge:\n")
        for m in hot:
            proj = m["project"] or "global"
            lines.append(
                f"- [{m['memory_type']} · {proj}] {m['content'][:100]}{'…' if len(m['content']) > 100 else ''}"
            )
            lines.append(f"  Accessed {m['access_count']} times")
        lines.append("")

    if not any([open_decisions, patterns, stale, hot]):
        lines.append("Not enough data yet for meaningful reflection. Keep building memories.")

    return "\n".join(lines)


@mcp.tool()
def forget(memory_id: str) -> str:
    """
    Delete a specific memory by ID.

    Call this when user says 'forget that', 'remove that memory', or 'that's no longer true'.
    Memory IDs are shown in search results and save confirmations.

    Args:
        memory_id: The UUID of the memory to delete
    """
    db.init_db()
    deleted = db.delete_memory(memory_id)
    if deleted:
        return f"Memory {memory_id} deleted."
    return f"Memory {memory_id} not found."


if __name__ == "__main__":
    db.init_db()
    mcp.run(transport="stdio")

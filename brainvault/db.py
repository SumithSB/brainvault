"""
brainvault/db.py — SQLite storage layer with FTS5 full-text search.
Database lives at ~/.brainvault/memory.db
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def get_db_path() -> Path:
    path = Path.home() / ".brainvault" / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def get_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL CHECK(memory_type IN ('profile','project','decision','pattern','note')),
                project TEXT,
                keywords TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'explicit',
                created_at TEXT DEFAULT (datetime('now')),
                last_accessed TEXT,
                access_count INTEGER DEFAULT 0,
                outcome TEXT
            );

            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                stack TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                last_active TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions_captured (
                session_path TEXT PRIMARY KEY,
                captured_at TEXT DEFAULT (datetime('now')),
                memory_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS memory_links (
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                relationship TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                PRIMARY KEY (from_id, to_id),
                FOREIGN KEY (from_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY (to_id) REFERENCES memories(id) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(
                content,
                keywords,
                project,
                memory_type,
                content='memories',
                content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS memories_ai
            AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, keywords, project, memory_type)
                VALUES (new.rowid, new.content, new.keywords, new.project, new.memory_type);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ad
            AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, keywords, project, memory_type)
                VALUES ('delete', old.rowid, old.content, old.keywords, old.project, old.memory_type);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_au
            AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, keywords, project, memory_type)
                VALUES ('delete', old.rowid, old.content, old.keywords, old.project, old.memory_type);
                INSERT INTO memories_fts(rowid, content, keywords, project, memory_type)
                VALUES (new.rowid, new.content, new.keywords, new.project, new.memory_type);
            END;
        """)
        # Migrations for existing databases
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations for existing databases."""
    existing_mem_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "outcome" not in existing_mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN outcome TEXT")

    existing_proj_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "last_active" not in existing_proj_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN last_active TEXT DEFAULT (datetime('now'))")


def save_memory(
    content: str,
    memory_type: str,
    project: str | None = None,
    keywords: list[str] | None = None,
    source: str = "explicit",
) -> str:
    memory_id = str(uuid.uuid4())
    keywords_json = json.dumps(keywords or _extract_keywords(content))
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO memories (id, content, memory_type, project, keywords, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, memory_type, project, keywords_json, source),
        )
        if project:
            conn.execute(
                "UPDATE projects SET last_active = datetime('now') WHERE name = ?",
                (project,),
            )
    return memory_id


def search_memories(query: str, project: str | None = None, limit: int = 5) -> list[dict]:
    with get_connection() as conn:
        # Quote each term individually so FTS5 does AND matching (not phrase matching).
        # Phrase quoting the whole query fails for multi-word queries where terms
        # appear in different columns or in a different order (e.g. "ssb49 dissertation"
        # vs project name "Dissertation-ssb49").
        terms = query.split()
        safe_query = " ".join(f'"{t.replace(chr(34), "")}"' for t in terms)
        try:
            if project:
                rows = conn.execute(
                    """
                    SELECT m.*, bm25(memories_fts) as rank
                    FROM memories_fts
                    JOIN memories m ON memories_fts.rowid = m.rowid
                    WHERE memories_fts MATCH ?
                    ORDER BY
                        CASE WHEN m.project = ? THEN 0
                             WHEN m.project IS NULL THEN 1
                             ELSE 2 END,
                        bm25(memories_fts)
                    LIMIT ?
                    """,
                    (safe_query, project, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.*, bm25(memories_fts) as rank
                    FROM memories_fts
                    JOIN memories m ON memories_fts.rowid = m.rowid
                    WHERE memories_fts MATCH ?
                    ORDER BY bm25(memories_fts)
                    LIMIT ?
                    """,
                    (safe_query, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 syntax error fallback — plain LIKE search
            like = f"%{query}%"
            if project:
                rows = conn.execute(
                    """
                    SELECT *, 0 as rank FROM memories
                    WHERE (content LIKE ? OR keywords LIKE ?)
                    ORDER BY
                        CASE WHEN project = ? THEN 0
                             WHEN project IS NULL THEN 1
                             ELSE 2 END,
                        created_at DESC
                    LIMIT ?
                    """,
                    (like, like, project, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *, 0 as rank FROM memories
                    WHERE content LIKE ? OR keywords LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (like, like, limit),
                ).fetchall()

        results = [dict(r) for r in rows]

        # update access stats and reflect in returned results
        if results:
            ids = [r["id"] for r in results]
            conn.execute(
                f"""
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed = datetime('now')
                WHERE id IN ({",".join("?" * len(ids))})
                """,
                ids,
            )
            for r in results:
                r["access_count"] = r["access_count"] + 1

        return results


def save_project(
    name: str,
    description: str,
    stack: list[str],
    status: str = "active",
    notes: str = "",
) -> None:
    stack_json = json.dumps(stack)
    with get_connection() as conn:
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
            (name, description, stack_json, status, notes),
        )


def get_project(name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def list_projects(status: str | None = "active") -> list[dict]:
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_project_memories(project: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE project = ? ORDER BY created_at DESC",
            (project,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_memory(memory_id: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0


def get_stats() -> dict:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_type = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
            ).fetchall()
        }
        by_project = {
            r[0] or "global": r[1]
            for r in conn.execute(
                "SELECT project, COUNT(*) FROM memories GROUP BY project"
            ).fetchall()
        }
        total_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        return {
            "total_memories": total,
            "by_type": by_type,
            "by_project": by_project,
            "total_projects": total_projects,
        }


def is_session_captured(session_path: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions_captured WHERE session_path = ?", (session_path,)
        ).fetchone()
        return row is not None


def mark_session_captured(session_path: str, memory_count: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions_captured (session_path, memory_count)
            VALUES (?, ?)
            """,
            (session_path, memory_count),
        )


def record_outcome(memory_id: str, outcome: str) -> bool:
    """Record the outcome of a past decision. Returns True if found and updated."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE memories SET outcome = ? WHERE id = ? AND memory_type = 'decision'",
            (outcome, memory_id),
        )
        return cursor.rowcount > 0


def get_reflection_data() -> dict:
    """
    Return cross-project data for gap and pattern analysis:
    - Open decisions (no outcome, older than 7 days)
    - Top repeated keywords across projects
    - Stale projects (no activity in 30+ days)
    - Most accessed memories
    """
    with get_connection() as conn:
        open_decisions = conn.execute(
            """
            SELECT id, content, project, created_at
            FROM memories
            WHERE memory_type = 'decision'
              AND outcome IS NULL
              AND created_at < datetime('now', '-7 days')
            ORDER BY created_at ASC
            LIMIT 10
            """
        ).fetchall()

        stale_projects = conn.execute(
            """
            SELECT name, description, last_active, updated_at
            FROM projects
            WHERE status = 'active'
              AND (
                last_active < datetime('now', '-30 days')
                OR (last_active IS NULL AND updated_at < datetime('now', '-30 days'))
              )
            ORDER BY last_active ASC
            """
        ).fetchall()

        hot_memories = conn.execute(
            """
            SELECT id, content, memory_type, project, access_count
            FROM memories
            WHERE access_count > 0
            ORDER BY access_count DESC
            LIMIT 5
            """
        ).fetchall()

        # Keyword frequency across all memories grouped by project
        all_keywords = conn.execute(
            "SELECT keywords, project FROM memories WHERE keywords != '[]'"
        ).fetchall()

    # Count keyword frequency across projects
    keyword_projects: dict[str, set] = {}
    for row in all_keywords:
        try:
            kws = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (json.JSONDecodeError, TypeError):
            continue
        proj = row[1] or "global"
        for kw in kws:
            if kw not in keyword_projects:
                keyword_projects[kw] = set()
            keyword_projects[kw].add(proj)

    # Cross-project patterns: keywords appearing in 2+ distinct projects
    cross_project = sorted(
        [(kw, list(projs)) for kw, projs in keyword_projects.items() if len(projs) >= 2],
        key=lambda x: -len(x[1]),
    )[:10]

    return {
        "open_decisions": [dict(r) for r in open_decisions],
        "stale_projects": [dict(r) for r in stale_projects],
        "hot_memories": [dict(r) for r in hot_memories],
        "cross_project_patterns": cross_project,
    }


def _extract_keywords(text: str) -> list[str]:
    """Simple keyword extraction — no NLP needed."""
    stopwords = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "we",
        "you",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "up",
        "so",
        "but",
        "and",
        "or",
        "not",
        "just",
        "also",
        "then",
        "when",
        "what",
        "which",
        "who",
        "how",
        "why",
        "all",
        "any",
        "some",
        "use",
        "used",
        "using",
        "make",
        "want",
        "going",
        "get",
        "go",
        "like",
    }
    import re

    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]*", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if len(w) >= 3 and w not in stopwords:
            freq[w] = freq.get(w, 0) + 1
    return sorted(freq, key=lambda w: -freq[w])[:10]

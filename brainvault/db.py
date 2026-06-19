"""
brainvault/db.py — SQLite storage layer with FTS5 full-text search.
Database lives at ~/.brainvault/memory.db
"""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

# Single source of truth for valid memory types (DB CHECK constraint mirrors this).
VALID_MEMORY_TYPES: frozenset[str] = frozenset(
    {"profile", "project", "decision", "pattern", "note"}
)

# Host / capture attribution for memories and session replay (additive column source_agent).
SYSTEM_SOURCE_AGENT = "system"
VALID_SOURCE_AGENTS: frozenset[str] = frozenset({"claude_code", "cursor", SYSTEM_SOURCE_AGENT})

# Hook note prefix for Cursor user-query blobs (disabled by default in capture).
CURSOR_QUERY_BLOB_PREFIX = "User queries in Cursor session"

# Wait up to 30s on connect for the DB lock; retry busy handlers up to 30s per statement.
_SQLITE_CONNECT_TIMEOUT_S = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30000


def get_db_path() -> Path:
    path = Path.home() / ".brainvault" / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def get_connection():
    conn = sqlite3.connect(get_db_path(), timeout=_SQLITE_CONNECT_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, AttributeError, sqlite3.OperationalError):
        # sqlite_vec not installed, or extension loading not supported on this build.
        pass
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
        conn.execute("ALTER TABLE projects ADD COLUMN last_active TEXT")
        conn.execute("UPDATE projects SET last_active = updated_at WHERE last_active IS NULL")

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "memory_vectors" not in tables:
        conn.execute("""
            CREATE TABLE memory_vectors (
                memory_id  TEXT PRIMARY KEY,
                embedding  BLOB NOT NULL,
                model      TEXT NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
            )
        """)

    if "git_commits_scanned" not in tables:
        conn.execute("""
            CREATE TABLE git_commits_scanned (
                repo_path   TEXT NOT NULL,
                commit_hash TEXT NOT NULL,
                scanned_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (repo_path, commit_hash)
            )
        """)

    existing_mem_cols2 = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "outcome_sentiment" not in existing_mem_cols2:
        conn.execute(
            "ALTER TABLE memories ADD COLUMN outcome_sentiment TEXT CHECK(outcome_sentiment IN ('positive','negative','mixed',NULL))"
        )

    if "code_entities" not in tables:
        conn.execute("""
            CREATE TABLE code_entities (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_path  TEXT NOT NULL,
                project    TEXT NOT NULL,
                file_path  TEXT NOT NULL,
                language   TEXT NOT NULL DEFAULT 'unknown',
                imports    TEXT NOT NULL DEFAULT '[]',
                indexed_at TEXT DEFAULT (datetime('now')),
                UNIQUE(repo_path, file_path)
            )
        """)
        conn.execute("CREATE INDEX idx_ce_project  ON code_entities(project)")
        conn.execute("CREATE INDEX idx_ce_repo_file ON code_entities(repo_path, file_path)")

    if "code_cochange" not in tables:
        conn.execute("""
            CREATE TABLE code_cochange (
                repo_path      TEXT NOT NULL,
                file_a         TEXT NOT NULL,
                file_b         TEXT NOT NULL,
                cochange_count INTEGER NOT NULL DEFAULT 1,
                last_cochange  TEXT,
                PRIMARY KEY (repo_path, file_a, file_b)
            )
        """)
        conn.execute("CREATE INDEX idx_cc_repo_a ON code_cochange(repo_path, file_a)")
        conn.execute("CREATE INDEX idx_cc_repo_b ON code_cochange(repo_path, file_b)")

    if "code_index_runs" not in tables:
        conn.execute("""
            CREATE TABLE code_index_runs (
                repo_path      TEXT PRIMARY KEY,
                project        TEXT NOT NULL,
                indexed_at     TEXT DEFAULT (datetime('now')),
                file_count     INTEGER NOT NULL DEFAULT 0,
                cochange_pairs INTEGER NOT NULL DEFAULT 0
            )
        """)

    if "session_events" not in tables:
        conn.execute("""
            CREATE TABLE session_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                project      TEXT,
                tool_name    TEXT NOT NULL,
                input_summary TEXT NOT NULL DEFAULT '',
                output_summary TEXT NOT NULL DEFAULT '',
                timestamp    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX idx_se_session  ON session_events(session_id)")
        conn.execute("CREATE INDEX idx_se_project   ON session_events(project)")
        conn.execute("CREATE INDEX idx_se_timestamp ON session_events(timestamp)")

    # Add indexes that were missing from the initial schema.
    existing_indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    if "idx_memories_source" not in existing_indexes:
        conn.execute("CREATE INDEX idx_memories_source ON memories(source)")
    if "idx_memories_project_created" not in existing_indexes:
        conn.execute("CREATE INDEX idx_memories_project_created ON memories(project, created_at)")

    # source_agent column — which coding-agent host produced the row.
    # Default 'claude_code' backfills pre-existing rows from before multi-agent
    # tagging. New rows set source_agent from the connecting host. Multi-agent installs tag each
    # new row at the capture/save site.
    for table in ("memories", "session_events", "sessions_captured"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "source_agent" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN source_agent TEXT NOT NULL DEFAULT 'claude_code'"
            )

    existing_mem_cols3 = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "content_hash" not in existing_mem_cols3:
        conn.execute("ALTER TABLE memories ADD COLUMN content_hash TEXT")

    existing_idx = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    if "idx_memories_content_hash" not in existing_idx:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash)"
        )

    sess_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions_captured)").fetchall()}
    if "transcript_bytes" not in sess_cols:
        conn.execute("ALTER TABLE sessions_captured ADD COLUMN transcript_bytes INTEGER")
    if "transcript_lines" not in sess_cols:
        conn.execute("ALTER TABLE sessions_captured ADD COLUMN transcript_lines INTEGER")


def memory_content_fingerprint(content: str) -> str:
    """Stable SHA-256 hex digest for deduplicating hook/git auto-captured memories."""
    norm = " ".join(content.split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def is_hook_capture_duplicate(
    content: str,
    project: str | None,
    *,
    source: str = "hook",
    source_agent: str = "claude_code",
) -> bool:
    """True if the same or equivalent hook memory was already stored."""
    fp = memory_content_fingerprint(content)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM memories
            WHERE source = ? AND source_agent = ?
              AND (content_hash = ? OR (content_hash IS NULL AND content = ? AND IFNULL(project, '') = IFNULL(?, '')))
            """,
            (source, source_agent, fp, content, project),
        ).fetchone()
        return row is not None


def save_memory(
    content: str,
    memory_type: str,
    project: str | None = None,
    keywords: list[str] | None = None,
    source: str = "explicit",
    source_agent: str = "claude_code",
    *,
    content_hash: str | None = None,
) -> str:
    if source_agent not in VALID_SOURCE_AGENTS:
        raise ValueError(
            f"Invalid source_agent {source_agent!r}; must be one of: {sorted(VALID_SOURCE_AGENTS)}"
        )
    memory_id = str(uuid.uuid4())
    keywords_json = json.dumps(keywords or _extract_keywords(content))
    ch = content_hash
    if ch is None and source in ("hook", "git"):
        ch = memory_content_fingerprint(content)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO memories (id, content, memory_type, project, keywords, source, source_agent, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, memory_type, project, keywords_json, source, source_agent, ch),
        )
        if project:
            conn.execute(
                "UPDATE projects SET last_active = datetime('now') WHERE name = ?",
                (project,),
            )
        _try_embed_and_store(conn, memory_id, content)
    return memory_id


def _try_embed_and_store(conn: sqlite3.Connection, memory_id: str, content: str) -> None:
    """Best-effort embedding. Silently skips if extras not installed or embedding fails."""
    try:
        from brainvault import embeddings as emb

        if not emb._is_available():
            return
        vector = emb.embed(content)
        blob = emb.serialize(vector)
        conn.execute(
            "INSERT OR REPLACE INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
            (memory_id, blob),
        )
    except Exception:
        # Never let embedding failure break a save
        pass


def search_memories(
    query: str,
    project: str | None = None,
    limit: int = 5,
    *,
    hybrid: bool = True,
) -> list[dict]:
    """
    Search memories using FTS5 keyword search, optionally combined with vector
    cosine similarity via Reciprocal Rank Fusion (RRF).

    Falls back to FTS5-only if semantic extras are not installed or vector search fails.
    Results include _fts_rank and _vec_rank provenance keys (stripped by callers if needed).
    """
    fts_results = _search_fts(query, project=project, limit=limit * 3)

    vec_results: list[dict] = []
    if hybrid:
        try:
            from brainvault import embeddings as emb

            if emb._is_available():
                vec_results = _search_vector(query, project=project, limit=limit * 3)
        except Exception:
            pass

    if vec_results:
        results = _rrf_merge(fts_results, vec_results, limit=limit)
    else:
        results = fts_results[:limit]

    _update_access_stats(results)
    return results


def _memory_quality_sort_key(doc: dict) -> tuple[int, int, int]:
    """Lower is better — used to boost high-signal memories in search ranking."""
    content = doc.get("content") or ""
    source = doc.get("source") or ""
    mtype = doc.get("memory_type") or "note"
    junk = 1 if source == "hook" and content.startswith(CURSOR_QUERY_BLOB_PREFIX) else 0
    source_rank = {"agent": 0, "git": 1, "bootstrap": 2, "hook": 3}.get(source, 4)
    type_rank = {"decision": 0, "pattern": 1, "project": 2, "profile": 3, "note": 4}.get(mtype, 5)
    return (junk, source_rank, type_rank)


_SEARCH_QUALITY_ORDER = """
                        CASE WHEN m.content LIKE 'User queries in Cursor session%'
                                  AND m.source = 'hook' THEN 1 ELSE 0 END,
                        CASE m.source
                            WHEN 'agent' THEN 0
                            WHEN 'git' THEN 1
                            WHEN 'bootstrap' THEN 2
                            WHEN 'hook' THEN 3
                            ELSE 4 END,
                        CASE m.memory_type
                            WHEN 'decision' THEN 0
                            WHEN 'pattern' THEN 1
                            WHEN 'project' THEN 2
                            WHEN 'profile' THEN 3
                            WHEN 'note' THEN 4 END,
"""


def _search_fts(query: str, project: str | None, limit: int) -> list[dict]:
    """FTS5 BM25 keyword search with project prioritisation and LIKE fallback."""
    with get_connection() as conn:
        # Quote each term individually so FTS5 does AND matching (not phrase matching).
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
                        {_SEARCH_QUALITY_ORDER}
                        bm25(memories_fts)
                    LIMIT ?
                    """.format(_SEARCH_QUALITY_ORDER=_SEARCH_QUALITY_ORDER),
                    (safe_query, project, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.*, bm25(memories_fts) as rank
                    FROM memories_fts
                    JOIN memories m ON memories_fts.rowid = m.rowid
                    WHERE memories_fts MATCH ?
                    ORDER BY
                        {_SEARCH_QUALITY_ORDER}
                        bm25(memories_fts)
                    LIMIT ?
                    """.format(_SEARCH_QUALITY_ORDER=_SEARCH_QUALITY_ORDER),
                    (safe_query, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            like_fallback_order = """
                        CASE WHEN content LIKE 'User queries in Cursor session%'
                                  AND source = 'hook' THEN 1 ELSE 0 END,
                        CASE source
                            WHEN 'agent' THEN 0 WHEN 'git' THEN 1
                            WHEN 'bootstrap' THEN 2 WHEN 'hook' THEN 3 ELSE 4 END,
                        CASE memory_type
                            WHEN 'decision' THEN 0 WHEN 'pattern' THEN 1
                            WHEN 'project' THEN 2 WHEN 'profile' THEN 3
                            WHEN 'note' THEN 4 END,
                        created_at DESC"""
            if project:
                rows = conn.execute(
                    f"""
                    SELECT *, 0 as rank FROM memories
                    WHERE (content LIKE ? OR keywords LIKE ?)
                    ORDER BY
                        CASE WHEN project = ? THEN 0
                             WHEN project IS NULL THEN 1
                             ELSE 2 END,
                        {like_fallback_order}
                    LIMIT ?
                    """,
                    (like, like, project, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT *, 0 as rank FROM memories
                    WHERE content LIKE ? OR keywords LIKE ?
                    ORDER BY {like_fallback_order}
                    LIMIT ?
                    """,
                    (like, like, limit),
                ).fetchall()

    results = [dict(r) for r in rows]
    for i, r in enumerate(results):
        r["_fts_rank"] = i
    return results


def _search_vector(query: str, project: str | None, limit: int) -> list[dict]:
    """Cosine similarity search using sqlite-vec. Returns results sorted by similarity."""
    from brainvault import embeddings as emb

    q_vec = emb.embed(query)
    q_blob = emb.serialize(q_vec)

    with get_connection() as conn:
        if project:
            rows = conn.execute(
                """
                SELECT m.*, vec_distance_cosine(v.embedding, ?) as distance
                FROM memory_vectors v
                JOIN memories m ON v.memory_id = m.id
                WHERE m.project = ? OR m.project IS NULL
                ORDER BY distance ASC
                LIMIT ?
                """,
                (q_blob, project, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.*, vec_distance_cosine(v.embedding, ?) as distance
                FROM memory_vectors v
                JOIN memories m ON v.memory_id = m.id
                ORDER BY distance ASC
                LIMIT ?
                """,
                (q_blob, limit),
            ).fetchall()

    results = [dict(r) for r in rows]
    for i, r in enumerate(results):
        r["_vec_rank"] = i
        r.pop("distance", None)
    return results


def _rrf_merge(
    fts_results: list[dict],
    vec_results: list[dict],
    limit: int,
    k: int = 60,
) -> list[dict]:
    """
    Reciprocal Rank Fusion over FTS5 and vector ranked lists.
    score(doc) = 1/(k + rank_fts) + 1/(k + rank_vec)
    Rank-position-only — no score scale mismatch between BM25 and cosine similarity.
    """
    scores: dict[str, float] = {}
    by_id: dict[str, dict] = {}

    for rank, doc in enumerate(fts_results):
        mid = doc["id"]
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
        by_id[mid] = {**doc, "_fts_rank": rank}

    for rank, doc in enumerate(vec_results):
        mid = doc["id"]
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
        if mid in by_id:
            by_id[mid]["_vec_rank"] = rank
        else:
            by_id[mid] = {**doc, "_vec_rank": rank}

    ranked = sorted(
        scores.keys(),
        key=lambda mid: (-scores[mid], *_memory_quality_sort_key(by_id[mid])),
    )
    return [by_id[mid] for mid in ranked[:limit]]


def _update_access_stats(results: list[dict]) -> None:
    """Increment access_count and last_accessed for the given result set."""
    if not results:
        return
    ids = [r["id"] for r in results]
    with get_connection() as conn:
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
        r["access_count"] = r.get("access_count", 0) + 1


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


def delete_project_memories(project_name: str) -> int:
    """Delete all memories for a project. Returns count deleted.
    FTS5, memory_vectors, and memory_links are cleaned up via triggers/CASCADE."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM memories WHERE project = ?", (project_name,))
        return cursor.rowcount


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


def get_status() -> dict:
    """Aggregate vault health data for the status command."""
    with get_connection() as conn:
        total_memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        by_type = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
            ).fetchall()
        }
        by_source = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT source, COUNT(*) FROM memories GROUP BY source"
            ).fetchall()
        }

        unembedded = conn.execute(
            """
            SELECT COUNT(*) FROM memories m
            LEFT JOIN memory_vectors v ON m.id = v.memory_id
            WHERE v.memory_id IS NULL
            """
        ).fetchone()[0]

        git_repos = conn.execute(
            "SELECT COUNT(DISTINCT repo_path) FROM git_commits_scanned"
        ).fetchone()[0]
        git_memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE source = 'git'"
        ).fetchone()[0]

        last_session = conn.execute(
            "SELECT captured_at, memory_count FROM sessions_captured ORDER BY captured_at DESC LIMIT 1"
        ).fetchone()

        open_decisions = conn.execute(
            """
            SELECT COUNT(*) FROM memories
            WHERE memory_type = 'decision'
              AND outcome IS NULL
              AND created_at < datetime('now', '-7 days')
            """
        ).fetchone()[0]

        stale_projects = conn.execute(
            """
            SELECT COUNT(*) FROM projects
            WHERE status = 'active'
              AND (last_active < datetime('now', '-30 days') OR last_active IS NULL)
            """
        ).fetchone()[0]

        cursor_query_blobs = conn.execute(
            """
            SELECT COUNT(*) FROM memories
            WHERE source = 'hook' AND content LIKE ?
            """,
            (f"{CURSOR_QUERY_BLOB_PREFIX}%",),
        ).fetchone()[0]
        embedded = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        accessed = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE IFNULL(access_count, 0) > 0"
        ).fetchone()[0]

    audit = audit_vault()

    return {
        "total_memories": total_memories,
        "by_type": by_type,
        "by_source": by_source,
        "unembedded": unembedded,
        "git_repos": git_repos,
        "git_memories": git_memories,
        "last_session_at": last_session[0][:10] if last_session else None,
        "last_session_memories": last_session[1] if last_session else 0,
        "open_decisions": open_decisions,
        "stale_projects": stale_projects,
        "cursor_query_blobs": cursor_query_blobs,
        "embedding_coverage_pct": audit["embedding_coverage_pct"],
        "access_rate_pct": audit["access_rate_pct"],
        "junk_hook_notes": audit["zero_access_hook_notes"],
        "unregistered_project_count": len(audit["unregistered_projects"]),
        "agent_vs_hook_ratio": audit["agent_vs_hook_ratio"],
    }


def update_memory(
    memory_id: str,
    content: str | None = None,
    memory_type: str | None = None,
    project: str | None = None,
    keywords: list[str] | None = None,
) -> bool:
    """
    Update fields of an existing memory. Returns True if found and updated.
    Re-embeds if content changes. FTS5 triggers handle index update automatically.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return False
        current = dict(row)

        new_content = content if content is not None else current["content"]
        new_type = memory_type if memory_type is not None else current["memory_type"]
        new_project = project if project is not None else current["project"]
        new_keywords = json.dumps(
            keywords
            if keywords is not None
            else _extract_keywords(new_content)
            if content is not None
            else json.loads(current["keywords"] or "[]")
        )

        new_hash = current.get("content_hash")
        if content is not None and current.get("source") in ("hook", "git"):
            new_hash = memory_content_fingerprint(new_content)

        cur = conn.execute(
            """
            UPDATE memories
            SET content = ?, memory_type = ?, project = ?, keywords = ?, content_hash = ?
            WHERE id = ?
            """,
            (new_content, new_type, new_project, new_keywords, new_hash, memory_id),
        )
        if cur.rowcount == 0:
            return False
        if new_project:
            conn.execute(
                "UPDATE projects SET last_active = datetime('now') WHERE name = ?",
                (new_project,),
            )
        # Re-embed if content changed
        if content is not None and content != current["content"]:
            # Remove old vector, will be re-created
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            _try_embed_and_store(conn, memory_id, new_content)

    return True


def is_session_captured(session_path: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions_captured WHERE session_path = ?", (session_path,)
        ).fetchone()
        return row is not None


def get_session_capture_row(session_path: str) -> sqlite3.Row | None:
    """Return the sessions_captured row for this transcript path, or None."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT session_path, captured_at, memory_count, source_agent, transcript_bytes, transcript_lines
            FROM sessions_captured
            WHERE session_path = ?
            """,
            (session_path,),
        ).fetchone()


def seed_session_transcript_stats(
    session_path: str, transcript_bytes: int, transcript_lines: int
) -> None:
    """Backfill transcript size/line stats for legacy rows (no duplicate memories)."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sessions_captured
            SET transcript_bytes = ?, transcript_lines = ?
            WHERE session_path = ?
            """,
            (transcript_bytes, transcript_lines, session_path),
        )


def mark_session_captured(
    session_path: str,
    memory_count: int,
    source_agent: str = "claude_code",
    *,
    transcript_bytes: int | None = None,
    transcript_lines: int | None = None,
) -> None:
    if source_agent not in VALID_SOURCE_AGENTS:
        raise ValueError(
            f"Invalid source_agent {source_agent!r}; must be one of: {sorted(VALID_SOURCE_AGENTS)}"
        )
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions_captured
                (session_path, captured_at, memory_count, source_agent, transcript_bytes, transcript_lines)
            VALUES (?, datetime('now'), ?, ?, ?, ?)
            """,
            (session_path, memory_count, source_agent, transcript_bytes, transcript_lines),
        )


def is_commit_scanned(repo_path: str, commit_hash: str) -> bool:
    """Return True if this commit has already been scanned for this repo."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM git_commits_scanned WHERE repo_path = ? AND commit_hash = ?",
            (repo_path, commit_hash),
        ).fetchone()
        return row is not None


def mark_commit_scanned(repo_path: str, commit_hash: str) -> None:
    """Record that a commit has been scanned. INSERT OR IGNORE preserves original scanned_at."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO git_commits_scanned (repo_path, commit_hash) VALUES (?, ?)",
            (repo_path, commit_hash),
        )


def get_git_scan_stats(repo_path: str | None = None) -> dict:
    """Return scanning statistics, optionally scoped to a single repo."""
    with get_connection() as conn:
        if repo_path:
            commits_scanned = conn.execute(
                "SELECT COUNT(*) FROM git_commits_scanned WHERE repo_path = ?",
                (repo_path,),
            ).fetchone()[0]
            git_memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source = 'git' AND project = ?",
                (repo_path,),
            ).fetchone()[0]
            return {"commits_scanned": commits_scanned, "git_memories": git_memories}
        else:
            commits_scanned = conn.execute("SELECT COUNT(*) FROM git_commits_scanned").fetchone()[0]
            git_memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source = 'git'"
            ).fetchone()[0]
            repos_scanned = conn.execute(
                "SELECT COUNT(DISTINCT repo_path) FROM git_commits_scanned"
            ).fetchone()[0]
            return {
                "commits_scanned": commits_scanned,
                "git_memories": git_memories,
                "repos_scanned": repos_scanned,
            }


def record_tool_event(
    session_id: str,
    tool_name: str,
    input_summary: str,
    output_summary: str = "",
    project: str | None = None,
    source_agent: str = "claude_code",
) -> None:
    """Append one PostToolUse event to the session_events ring buffer."""
    if source_agent not in VALID_SOURCE_AGENTS:
        raise ValueError(
            f"Invalid source_agent {source_agent!r}; must be one of: {sorted(VALID_SOURCE_AGENTS)}"
        )
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO session_events (session_id, project, tool_name, input_summary, output_summary, source_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                project,
                tool_name,
                input_summary[:500],
                output_summary[:200],
                source_agent,
            ),
        )


def get_session_timeline(session_id: str) -> list[dict]:
    """Return all events for a session in chronological order."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, project, tool_name, input_summary, output_summary, timestamp,
                   source_agent
            FROM session_events
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_activity(
    project: str | None = None,
    days: int = 7,
    limit_sessions: int = 5,
) -> dict:
    """
    Return a compact activity index for the most recent sessions.

    Returns:
        {
          "sessions": [
            {"session_id": str, "project": str|None, "event_count": int,
             "tools": [str, ...], "last_event": str, "first_event": str}
          ],
          "total_events": int,
          "period_days": int,
        }
    """
    since = f"datetime('now', '-{int(days)} days')"
    project_filter = "AND project = ?" if project else ""
    params: list = [project] if project else []

    with get_connection() as conn:
        total_events = conn.execute(
            f"SELECT COUNT(*) FROM session_events WHERE timestamp >= {since} {project_filter}",
            params,
        ).fetchone()[0]

        # Get recent session IDs ordered by most recent activity
        session_rows = conn.execute(
            f"""
            SELECT session_id, project, COUNT(*) as event_count,
                   MIN(timestamp) as first_event, MAX(timestamp) as last_event
            FROM session_events
            WHERE timestamp >= {since} {project_filter}
            GROUP BY session_id
            ORDER BY last_event DESC
            LIMIT ?
            """,
            params + [limit_sessions],
        ).fetchall()

        sessions = []
        for row in session_rows:
            # Get distinct tools used in this session
            tool_rows = conn.execute(
                "SELECT DISTINCT tool_name FROM session_events WHERE session_id = ? ORDER BY id",
                (row["session_id"],),
            ).fetchall()
            sessions.append(
                {
                    "session_id": row["session_id"],
                    "project": row["project"],
                    "event_count": row["event_count"],
                    "tools": [t[0] for t in tool_rows],
                    "first_event": row["first_event"],
                    "last_event": row["last_event"],
                }
            )

    return {"sessions": sessions, "total_events": total_events, "period_days": days}


def prune_old_events(days: int = 90) -> int:
    """Delete session_events older than `days` days. Returns rows deleted."""
    with get_connection() as conn:
        cursor = conn.execute(
            f"DELETE FROM session_events WHERE timestamp < datetime('now', '-{int(days)} days')"
        )
        return cursor.rowcount


def get_unembedded_memories() -> list[dict]:
    """Return all memories that have no row in memory_vectors."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.content FROM memories m
            LEFT JOIN memory_vectors v ON m.id = v.memory_id
            WHERE v.memory_id IS NULL
            """
        ).fetchall()
    return [dict(r) for r in rows]


def store_embedding(memory_id: str, vector: list[float]) -> None:
    """Persist a precomputed embedding for a memory."""
    from brainvault import embeddings as emb

    blob = emb.serialize(vector)
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
            (memory_id, blob),
        )


def count_embedded() -> int:
    """Return number of memories that have stored embeddings."""
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]


def record_outcome(
    memory_id: str,
    outcome: str,
    sentiment: str | None = None,
) -> bool:
    """Record the outcome (and optional sentiment) of a past decision. Returns True if found."""
    valid_sentiments = {"positive", "negative", "mixed", None}
    if sentiment not in valid_sentiments:
        sentiment = None
    with get_connection() as conn:
        # First check whether the memory exists at all to give a precise error
        row = conn.execute("SELECT memory_type FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return False
        if row[0] != "decision":
            # Memory exists but is the wrong type — treat as not-updatable (False)
            return False
        conn.execute(
            "UPDATE memories SET outcome = ?, outcome_sentiment = ? WHERE id = ?",
            (outcome, sentiment, memory_id),
        )
        return True


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

        # Sentiment breakdown for decisions with outcomes
        sentiment_rows = conn.execute(
            """
            SELECT outcome_sentiment, COUNT(*) as cnt
            FROM memories
            WHERE memory_type = 'decision' AND outcome IS NOT NULL
            GROUP BY outcome_sentiment
            """
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

    sentiment_summary = {(row[0] or "unrated"): row[1] for row in sentiment_rows}

    return {
        "open_decisions": [dict(r) for r in open_decisions],
        "stale_projects": [dict(r) for r in stale_projects],
        "hot_memories": [dict(r) for r in hot_memories],
        "cross_project_patterns": cross_project,
        "outcome_sentiment_summary": sentiment_summary,
    }


def _extract_keywords(text: str) -> list[str]:
    """Simple keyword extraction — no NLP needed. Caps at 5 000 chars to stay fast."""
    text = text[:5000]
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
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]*", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if len(w) >= 3 and w not in stopwords:
            freq[w] = freq.get(w, 0) + 1
    return sorted(freq, key=lambda w: -freq[w])[:10]


# ---------------------------------------------------------------------------
# Code intelligence — structural index
# ---------------------------------------------------------------------------


def index_repo_files(
    repo_path: str,
    project: str,
    files: list[dict],
) -> int:
    """
    Replace all code_entities for repo_path with a fresh batch.

    Each dict in files must have: file_path (str), language (str), imports (list[str]).
    Returns the number of rows inserted.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM code_entities WHERE repo_path = ?", (repo_path,))
        if not files:
            return 0
        conn.executemany(
            """
            INSERT INTO code_entities (repo_path, project, file_path, language, imports)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    repo_path,
                    project,
                    f["file_path"],
                    f["language"],
                    json.dumps(f.get("imports", [])),
                )
                for f in files
            ],
        )
    return len(files)


def bulk_record_cochange(
    repo_path: str,
    pairs: list[tuple[str, str, int, str | None]],
) -> int:
    """
    Replace all co-change pairs for repo_path with a fresh batch.

    Each tuple: (file_a, file_b, count, last_date).
    Canonical order (file_a < file_b) is enforced internally.
    Returns the number of pairs inserted.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM code_cochange WHERE repo_path = ?", (repo_path,))
        if not pairs:
            return 0
        normalised = [
            (repo_path, min(a, b), max(a, b), count, last_date) for a, b, count, last_date in pairs
        ]
        conn.executemany(
            """
            INSERT INTO code_cochange (repo_path, file_a, file_b, cochange_count, last_cochange)
            VALUES (?, ?, ?, ?, ?)
            """,
            normalised,
        )
    return len(normalised)


def update_code_index_run(
    repo_path: str,
    project: str,
    file_count: int,
    cochange_pairs: int,
) -> None:
    """Upsert the code_index_runs row for a repo after indexing completes."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO code_index_runs (repo_path, project, file_count, cochange_pairs)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(repo_path) DO UPDATE SET
                project        = excluded.project,
                indexed_at     = datetime('now'),
                file_count     = excluded.file_count,
                cochange_pairs = excluded.cochange_pairs
            """,
            (repo_path, project, file_count, cochange_pairs),
        )


def is_repo_indexed(repo_path: str) -> bool:
    """Return True if this repo has a code_index_runs row."""
    with get_connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM code_index_runs WHERE repo_path = ?", (repo_path,)
            ).fetchone()
            is not None
        )


def get_project_repo_path(project: str) -> str | None:
    """Return the repo_path for a project from code_index_runs, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT repo_path FROM code_index_runs WHERE project = ? LIMIT 1",
            (project,),
        ).fetchone()
        return row[0] if row else None


def get_code_context_data(
    project: str,
    query: str,
    limit: int = 5,
) -> dict:
    """
    Multi-signal ranked context: relevant memories + ranked files from structural index.

    Signals combined:
      1. Memories matching the query (FTS5, scoped to project)
      2. Files extracted from matching git commit memories
      3. Co-change partners of those files
      4. Files matching query terms via LIKE on file_path

    Returns:
        {
          "memories": [...],       # relevant memory dicts
          "ranked_files": [...],   # {file_path, language, score, reason, cochange_partners}
          "project": str,
          "query": str,
        }
    """
    memories = _search_fts(query, project=project, limit=limit * 3)
    _update_access_stats(memories)

    # Extract file paths mentioned in git commit memories
    git_files: list[str] = []
    for m in memories:
        if m.get("source") == "git":
            match = re.search(r"Files:\s*(.+)", m.get("content", ""))
            if match:
                git_files.extend(f.strip() for f in match.group(1).split(",") if f.strip())

    repo_path = get_project_repo_path(project)

    # Build scored file dict: file_path → {score, reason, cochange_partners, language}
    scored: dict[str, dict] = {}

    def _add(fp: str, score: float, reason: str) -> None:
        if fp not in scored:
            scored[fp] = {
                "score": 0.0,
                "reasons": [],
                "cochange_partners": [],
                "language": "unknown",
            }
        scored[fp]["score"] += score
        scored[fp]["reasons"].append(reason)

    # Signal 1: files from git memories (high weight — direct evidence)
    for fp in git_files:
        _add(fp, 3.0, "mentioned in matching git commit")

    # Signal 2: co-change partners (medium weight)
    if repo_path and git_files:
        with get_connection() as conn:
            for fp in set(git_files):
                rows = conn.execute(
                    """
                    SELECT CASE WHEN file_a = ? THEN file_b ELSE file_a END as partner,
                           cochange_count
                    FROM code_cochange
                    WHERE repo_path = ? AND (file_a = ? OR file_b = ?)
                    ORDER BY cochange_count DESC
                    LIMIT 5
                    """,
                    (fp, repo_path, fp, fp),
                ).fetchall()
                for row in rows:
                    partner, count = row[0], row[1]
                    _add(partner, min(count / 5.0, 2.0), f"co-changes with {fp} ({count}×)")
                    # Annotate the source file with its partners too
                    if fp in scored:
                        scored[fp]["cochange_partners"].append(partner)

    # Signal 3: query term LIKE match on file_path (low weight — structural match)
    if repo_path:
        terms = [t for t in query.lower().split() if len(t) >= 3]
        with get_connection() as conn:
            for term in terms[:4]:  # cap at 4 terms to avoid slow queries
                rows = conn.execute(
                    """
                    SELECT file_path, language FROM code_entities
                    WHERE repo_path = ? AND file_path LIKE ?
                    LIMIT 10
                    """,
                    (repo_path, f"%{term}%"),
                ).fetchall()
                for row in rows:
                    _add(row[0], 1.0, f"file name matches '{term}'")
                    if row[0] in scored:
                        scored[row[0]]["language"] = row[1]

    # Enrich with language from code_entities where not already set
    if repo_path and scored:
        unknown_paths = [fp for fp, v in scored.items() if v["language"] == "unknown"]
        if unknown_paths:
            placeholders = ",".join("?" * len(unknown_paths))
            with get_connection() as conn:
                rows = conn.execute(
                    f"SELECT file_path, language FROM code_entities WHERE repo_path = ? AND file_path IN ({placeholders})",
                    [repo_path, *unknown_paths],
                ).fetchall()
            for row in rows:
                scored[row[0]]["language"] = row[1]

    ranked = sorted(scored.items(), key=lambda x: -x[1]["score"])[:limit]
    ranked_files = [
        {
            "file_path": fp,
            "language": v["language"],
            "score": round(v["score"], 2),
            "reason": "; ".join(dict.fromkeys(v["reasons"])),  # deduplicated, ordered
            "cochange_partners": v["cochange_partners"][:3],
        }
        for fp, v in ranked
    ]

    return {
        "memories": memories[:limit],
        "ranked_files": ranked_files,
        "project": project,
        "query": query,
    }


# --- Vault audit & prune -------------------------------------------------------

_PROTECTED_MEMORY_TYPES = frozenset({"decision", "pattern", "profile"})
_PROTECTED_SOURCES = frozenset({"agent"})


def _is_protected_memory(row: sqlite3.Row | dict) -> bool:
    """Memories that must never be auto-pruned."""
    data = dict(row) if not isinstance(row, dict) else row
    if data.get("source") in _PROTECTED_SOURCES:
        return True
    if data.get("memory_type") in _PROTECTED_MEMORY_TYPES:
        return True
    if int(data.get("access_count") or 0) > 0:
        return True
    return False


def audit_vault() -> dict:
    """Return vault health metrics for audit/prune decisions."""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_source = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT source, COUNT(*) FROM memories GROUP BY source"
            ).fetchall()
        }
        by_type = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
            ).fetchall()
        }
        cursor_query_blobs = conn.execute(
            """
            SELECT COUNT(*) FROM memories
            WHERE source = 'hook'
              AND content LIKE ?
            """,
            (f"{CURSOR_QUERY_BLOB_PREFIX}%",),
        ).fetchone()[0]
        zero_access_hook_notes = conn.execute(
            """
            SELECT COUNT(*) FROM memories
            WHERE source = 'hook'
              AND memory_type = 'note'
              AND IFNULL(access_count, 0) = 0
            """
        ).fetchone()[0]
        duplicate_hash_groups = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT content_hash FROM memories
                WHERE content_hash IS NOT NULL
                GROUP BY content_hash
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        duplicate_hash_rows = conn.execute(
            """
            SELECT COALESCE(SUM(cnt - 1), 0) FROM (
                SELECT COUNT(*) AS cnt FROM memories
                WHERE content_hash IS NOT NULL
                GROUP BY content_hash
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        embedded = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        accessed = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE IFNULL(access_count, 0) > 0"
        ).fetchone()[0]
        open_decisions = conn.execute(
            """
            SELECT COUNT(*) FROM memories
            WHERE memory_type = 'decision' AND outcome IS NULL
            """
        ).fetchone()[0]
        registered = {r[0] for r in conn.execute("SELECT name FROM projects").fetchall()}
        unregistered_rows = conn.execute(
            """
            SELECT project, COUNT(*) AS cnt FROM memories
            WHERE project IS NOT NULL AND project != ''
            GROUP BY project
            ORDER BY cnt DESC
            """
        ).fetchall()
        unregistered_projects = [
            {"project": r[0], "memory_count": r[1]}
            for r in unregistered_rows
            if r[0] not in registered
        ]
        hook_note_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE source = 'hook' AND memory_type = 'note'"
        ).fetchone()[0]

    agent_count = by_source.get("agent", 0)
    hook_count = by_source.get("hook", 0)
    high_signal = agent_count + by_source.get("git", 0)

    return {
        "total_memories": total,
        "by_source": by_source,
        "by_type": by_type,
        "cursor_query_blobs": cursor_query_blobs,
        "zero_access_hook_notes": zero_access_hook_notes,
        "duplicate_hash_groups": duplicate_hash_groups,
        "duplicate_hash_removable": duplicate_hash_rows,
        "embedded_count": embedded,
        "embedding_coverage_pct": round(100 * embedded / total, 1) if total else 0.0,
        "accessed_count": accessed,
        "access_rate_pct": round(100 * accessed / total, 1) if total else 0.0,
        "open_decisions_without_outcome": open_decisions,
        "unregistered_projects": unregistered_projects,
        "agent_vs_hook_ratio": f"{agent_count}:{hook_count}",
        "high_signal_share_pct": round(100 * high_signal / total, 1) if total else 0.0,
        "hook_note_share_pct": round(100 * hook_note_count / total, 1) if total else 0.0,
    }


def _duplicate_hash_ids_to_prune(conn: sqlite3.Connection, *, keep: str = "newest") -> list[str]:
    """Return memory IDs to delete for duplicate content_hash groups."""
    groups = conn.execute(
        """
        SELECT content_hash FROM memories
        WHERE content_hash IS NOT NULL
        GROUP BY content_hash
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    to_delete: list[str] = []
    for (content_hash,) in groups:
        rows = conn.execute(
            """
            SELECT id, access_count, created_at FROM memories
            WHERE content_hash = ?
            """,
            (content_hash,),
        ).fetchall()
        candidates = [dict(r) for r in rows if not _is_protected_memory(r)]
        if len(candidates) <= 1:
            continue
        if keep == "access_count":
            candidates.sort(
                key=lambda r: (int(r["access_count"] or 0), r["created_at"] or ""),
                reverse=True,
            )
        else:
            candidates.sort(key=lambda r: r["created_at"] or "", reverse=True)
        keeper_id = candidates[0]["id"]
        for row in candidates[1:]:
            if row["id"] != keeper_id:
                to_delete.append(row["id"])
    return to_delete


def prune_memories(
    *,
    dry_run: bool = True,
    cursor_query_blobs: bool = False,
    duplicate_hash: bool = False,
    duplicate_keep: str = "newest",
    source: str | None = None,
    memory_type: str | None = None,
    content_pattern: str | None = None,
    vacuum: bool = False,
) -> dict:
    """
    Delete low-value memories matching prune rules.

    Protected from all rules: source=agent, type in decision/pattern/profile,
    access_count > 0.

    Returns dict with keys: dry_run, deleted, candidate_ids, candidates_preview.
    """
    if duplicate_keep not in ("newest", "access_count"):
        duplicate_keep = "newest"

    ids_to_delete: set[str] = set()
    deleted = 0
    preview: list[sqlite3.Row] = []

    with get_connection() as conn:
        if cursor_query_blobs:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE source = 'hook'
                  AND content LIKE ?
                """,
                (f"{CURSOR_QUERY_BLOB_PREFIX}%",),
            ).fetchall()
            for row in rows:
                if not _is_protected_memory(row):
                    ids_to_delete.add(row["id"])

        if duplicate_hash:
            for mid in _duplicate_hash_ids_to_prune(conn, keep=duplicate_keep):
                ids_to_delete.add(mid)

        if source or memory_type or content_pattern:
            clauses: list[str] = []
            params: list[str] = []
            if source:
                clauses.append("source = ?")
                params.append(source)
            if memory_type:
                clauses.append("memory_type = ?")
                params.append(memory_type)
            if content_pattern:
                clauses.append("content LIKE ?")
                params.append(content_pattern)
            where = " AND ".join(clauses)
            rows = conn.execute(f"SELECT * FROM memories WHERE {where}", params).fetchall()
            for row in rows:
                if not _is_protected_memory(row):
                    ids_to_delete.add(row["id"])

        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            preview = conn.execute(
                f"""
                SELECT id, memory_type, source, project, substr(content, 1, 80) AS preview
                FROM memories WHERE id IN ({placeholders})
                LIMIT 20
                """,
                list(ids_to_delete),
            ).fetchall()

        if not dry_run and ids_to_delete:
            for mid in ids_to_delete:
                cur = conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
                deleted += cur.rowcount

    if vacuum and not dry_run and deleted:
        vacuum_db()

    return {
        "dry_run": dry_run,
        "deleted": deleted if not dry_run else 0,
        "candidate_count": len(ids_to_delete),
        "candidate_ids": sorted(ids_to_delete),
        "candidates_preview": [dict(r) for r in preview],
    }


def vacuum_db() -> None:
    """Reclaim disk space after bulk deletes (must run outside a transaction)."""
    path = get_db_path()
    conn = sqlite3.connect(path, timeout=_SQLITE_CONNECT_TIMEOUT_S)
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

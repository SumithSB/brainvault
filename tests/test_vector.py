"""
tests/test_vector.py — Tests for vector search and hybrid ranking.
Uses fake embeddings from conftest — no model download required.
"""

from brainvault import db


def test_save_memory_creates_vector_row():
    db.save_memory("FastAPI is preferred for async APIs", "pattern")
    assert db.count_embedded() == 1


def test_multiple_saves_create_multiple_vector_rows():
    db.save_memory("FastAPI is preferred for async APIs", "pattern")
    db.save_memory("PostgreSQL for relational data with strong schema", "decision")
    assert db.count_embedded() == 2


def test_search_returns_results_with_hybrid():
    db.save_memory("FastAPI is preferred for async APIs", "pattern")
    db.save_memory("PostgreSQL for relational data", "decision")
    # Use a term present in the stored content so FTS5 also finds it
    results = db.search_memories("FastAPI async")
    assert len(results) >= 1


def test_get_unembedded_memories(monkeypatch):
    # Bypass embedding at save time so we get an unembedded memory
    monkeypatch.setattr(db, "_try_embed_and_store", lambda *a, **kw: None)
    db.save_memory("No embedding for this one", "note")
    pending = db.get_unembedded_memories()
    assert len(pending) == 1
    assert pending[0]["content"] == "No embedding for this one"


def test_store_embedding_removes_from_unembedded(monkeypatch):
    monkeypatch.setattr(db, "_try_embed_and_store", lambda *a, **kw: None)
    memory_id = db.save_memory("Will embed later", "note")
    assert len(db.get_unembedded_memories()) == 1

    import brainvault.embeddings as emb

    vector = emb.embed("Will embed later")
    db.store_embedding(memory_id, vector)
    assert len(db.get_unembedded_memories()) == 0
    assert db.count_embedded() == 1


def test_rrf_merge_deduplicates_and_promotes():
    fts = [
        {"id": "a", "content": "only in fts", "_fts_rank": 0},
        {"id": "b", "content": "in both", "_fts_rank": 1},
    ]
    vec = [
        {"id": "b", "content": "in both", "_vec_rank": 0},
        {"id": "c", "content": "only in vec", "_vec_rank": 1},
    ]
    merged = db._rrf_merge(fts, vec, limit=3)
    ids = [m["id"] for m in merged]

    # b appears in both — should appear exactly once
    assert ids.count("b") == 1
    # b should be ranked first (highest combined RRF score)
    assert ids[0] == "b"
    # All three distinct docs present
    assert set(ids) == {"a", "b", "c"}


def test_fallback_to_fts_when_unavailable(monkeypatch):
    """If _is_available returns False, search should still work via FTS5."""
    import brainvault.embeddings as emb

    monkeypatch.setattr(emb, "_is_available", lambda: False)
    db.save_memory("JWT auth decision for stateless API", "decision")
    results = db.search_memories("JWT")
    assert len(results) == 1
    assert results[0]["content"] == "JWT auth decision for stateless API"


def test_project_filter_in_vector_search():
    db.save_memory("auth pattern for pluto", "decision", project="pluto")
    db.save_memory("auth pattern for ivy", "decision", project="ivy")
    results = db.search_memories("auth", project="pluto")
    assert len(results) >= 1
    # First result must be the scoped project (pluto prioritised over ivy)
    assert results[0]["project"] == "pluto"


def test_search_without_vectors_still_works(monkeypatch):
    """Vector search raises an error — should silently fall back to FTS5."""
    import brainvault.embeddings as emb

    monkeypatch.setattr(emb, "embed", lambda t: (_ for _ in ()).throw(RuntimeError("model error")))
    db.save_memory("PostgreSQL decision", "decision")
    results = db.search_memories("PostgreSQL")
    assert len(results) >= 1

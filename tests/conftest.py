"""
tests/conftest.py — Shared pytest fixtures for brainvault tests.
"""

import math

import pytest


def _fake_embed(text: str) -> list[float]:
    """Deterministic fake embedding — hash text to a unit vector. No model download."""
    h = abs(hash(text)) % (10**9)
    raw = [(h >> (i % 32) & 0xFF) / 255.0 for i in range(384)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _fake_embed_batch(texts: list[str]) -> list[list[float]]:
    return [_fake_embed(t) for t in texts]


@pytest.fixture(autouse=True)
def mock_embeddings(monkeypatch):
    """
    Patch embeddings so tests never download models.
    _is_available returns True so vector code paths are fully exercised.
    Must run before tmp_db so the patched embed is in place when init_db runs.
    """
    import brainvault.embeddings as emb

    monkeypatch.setattr(emb, "embed", _fake_embed)
    monkeypatch.setattr(emb, "embed_batch", _fake_embed_batch)
    monkeypatch.setattr(emb, "_is_available", lambda: True)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch, mock_embeddings):
    """Redirect all DB operations to a temp file for each test."""
    db_path = tmp_path / "test_memory.db"
    monkeypatch.setattr("brainvault.db.get_db_path", lambda: db_path)
    from brainvault import db

    db.init_db()
    yield db_path

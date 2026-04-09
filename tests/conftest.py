"""
tests/conftest.py — Shared pytest fixtures for brainvault tests.
"""

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect all DB operations to a temp file for each test."""
    db_path = tmp_path / "test_memory.db"
    monkeypatch.setattr("brainvault.db.get_db_path", lambda: db_path)
    from brainvault import db

    db.init_db()
    yield db_path

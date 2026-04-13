"""
brainvault/embeddings.py — Local embedding support for semantic search.

Uses fastembed (ONNX, no PyTorch) with BAAI/bge-small-en-v1.5 (~130MB, downloaded once).
Vectors stored in SQLite via sqlite-vec.

Both dependencies are optional extras: pip install 'brainvault[semantic]'
If not installed, _is_available() returns False and all callers fall back to FTS5-only search.
"""

from __future__ import annotations

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_encoder = None  # module-level singleton, lazy-loaded on first use


def _is_available() -> bool:
    """Return True if both fastembed and sqlite-vec are importable."""
    try:
        import fastembed  # noqa: F401
        import sqlite_vec  # noqa: F401

        return True
    except ImportError:
        return False


def get_encoder():
    """Lazy-load fastembed TextEmbedding, caching the instance for the process lifetime."""
    global _encoder
    if _encoder is None:
        from fastembed import TextEmbedding

        _encoder = TextEmbedding(model_name=EMBEDDING_MODEL)
    return _encoder


def embed(text: str) -> list[float]:
    """
    Return a 384-dim float list for a single string.
    Raises ImportError if fastembed is not installed.
    """
    encoder = get_encoder()
    vectors = list(encoder.embed([text]))
    return vectors[0].tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embed multiple texts, returning a list of float lists.
    Uses fastembed's batched path for efficiency.
    """
    encoder = get_encoder()
    return [v.tolist() for v in encoder.embed(texts)]


def serialize(vector: list[float]) -> bytes:
    """Serialise a float32 list to little-endian bytes for sqlite-vec storage."""
    import struct

    return struct.pack(f"<{len(vector)}f", *vector)


def deserialize(blob: bytes) -> list[float]:
    """Deserialise bytes from sqlite-vec back to a Python list."""
    import struct

    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))

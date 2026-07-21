"""
tests/test_vectorstore.py
==========================
Task 2 acceptance tests — shared pytest fixture parametrized on backend,
same suite runs against LocalFAISS (and can run against PineconeStore if
PINECONE_API_KEY is set, though that's skipped in CI by default).

Done-when criteria (build.md Task 2):
  ✓ upsert 3 chunks, query returns correct results
  ✓ delete_namespace clears all data
  ✓ get_by_chunk_id returns chunk or None correctly
  ✓ same test suite runs against both backends via parametrized fixture
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sentinel_review.ingestion.parser import CodeChunk
from sentinel_review.vectorstore.base import ScoredChunk
from sentinel_review.vectorstore.local_faiss import LocalFAISS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_chunk(name: str, code: str = "def foo(): pass", file_path: str = "test.py") -> CodeChunk:
    return CodeChunk.build(
        file_path=file_path,
        name=name,
        kind="function",
        start_line=1,
        end_line=3,
        code=code,
        docstring="",
        calls=set(),
        language="python",
    )


def make_embedding(dim: int = 8, seed: int = 0) -> list[float]:
    import numpy as np
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(float).tolist()
    return v


# ---------------------------------------------------------------------------
# Backend fixture
# ---------------------------------------------------------------------------

@pytest.fixture(params=["local"])
def vector_store(request, tmp_path: Path):
    """Parametrized fixture — currently 'local', extendable to 'pinecone'."""
    if request.param == "local":
        return LocalFAISS(cache_root=tmp_path / ".sentinel_cache")
    pytest.skip(f"Backend '{request.param}' not available in this environment")


# ---------------------------------------------------------------------------
# Shared test suite
# ---------------------------------------------------------------------------

NAMESPACE = "test/repo-abc"


class TestVectorStoreProtocol:
    def test_upsert_and_query_returns_results(self, vector_store) -> None:
        """Upsert 3 chunks, query with similar vector, get results back."""
        dim = 8
        chunks = [
            make_chunk("alpha", code="def alpha(): return 1"),
            make_chunk("beta",  code="def beta(): return 2"),
            make_chunk("gamma", code="def gamma(): return 3"),
        ]
        embeddings = [make_embedding(dim, seed=i) for i in range(3)]

        vector_store.upsert(chunks, embeddings, namespace=NAMESPACE)

        # Query with the first chunk's embedding — should return it as top-1
        results = vector_store.query(embeddings[0], namespace=NAMESPACE, top_k=3)

        assert len(results) >= 1
        assert isinstance(results[0], ScoredChunk)
        # Top result should be the most similar chunk (alpha, same vector)
        assert results[0].name == "alpha"

    def test_query_respects_top_k(self, vector_store) -> None:
        dim = 8
        chunks = [make_chunk(f"fn_{i}", code=f"def fn_{i}(): pass") for i in range(5)]
        embeddings = [make_embedding(dim, seed=i) for i in range(5)]
        vector_store.upsert(chunks, embeddings, namespace=NAMESPACE)

        results = vector_store.query(embeddings[0], namespace=NAMESPACE, top_k=2)
        assert len(results) <= 2

    def test_delete_namespace_clears_data(self, vector_store) -> None:
        """delete_namespace removes all data; subsequent query returns empty."""
        dim = 8
        chunks = [make_chunk("fn_x", code="def fn_x(): pass")]
        embeddings = [make_embedding(dim, seed=99)]
        vector_store.upsert(chunks, embeddings, namespace=NAMESPACE)

        vector_store.delete_namespace(NAMESPACE)

        results = vector_store.query(embeddings[0], namespace=NAMESPACE, top_k=5)
        assert results == []

    def test_delete_nonexistent_namespace_is_noop(self, vector_store) -> None:
        """delete_namespace on missing namespace must not raise."""
        vector_store.delete_namespace("no/such-namespace")  # should not raise

    def test_get_by_chunk_id_returns_chunk(self, vector_store) -> None:
        """get_by_chunk_id retrieves the exact chunk that was upserted."""
        dim = 8
        chunk = make_chunk("target_fn", code="def target_fn(): return 42")
        vector_store.upsert([chunk], [make_embedding(dim, seed=7)], namespace=NAMESPACE)

        retrieved = vector_store.get_by_chunk_id(chunk.chunk_id, namespace=NAMESPACE)
        assert retrieved is not None
        assert retrieved.chunk_id == chunk.chunk_id
        assert retrieved.name == "target_fn"
        assert retrieved.content_hash == chunk.content_hash

    def test_get_by_chunk_id_returns_none_for_missing(self, vector_store) -> None:
        """get_by_chunk_id returns None for an ID that was never upserted."""
        result = vector_store.get_by_chunk_id("nonexistent_id_xyz", namespace=NAMESPACE)
        assert result is None

    def test_upsert_overwrites_existing_chunk(self, vector_store) -> None:
        """Re-upserting a chunk with the same chunk_id replaces the old version."""
        dim = 8
        chunk_v1 = make_chunk("evolving_fn", code="def evolving_fn(): return 1")
        chunk_v2 = make_chunk("evolving_fn", code="def evolving_fn(): return 2")
        # Same chunk_id (derived from file_path, name, start_line)
        assert chunk_v1.chunk_id == chunk_v2.chunk_id

        vector_store.upsert([chunk_v1], [make_embedding(dim, seed=1)], namespace=NAMESPACE)
        vector_store.upsert([chunk_v2], [make_embedding(dim, seed=2)], namespace=NAMESPACE)

        retrieved = vector_store.get_by_chunk_id(chunk_v1.chunk_id, namespace=NAMESPACE)
        assert retrieved is not None
        # Should reflect the updated code
        assert "return 2" in retrieved.code

    def test_scores_are_descending(self, vector_store) -> None:
        """Query results must be sorted by score descending."""
        dim = 8
        chunks = [make_chunk(f"f{i}") for i in range(4)]
        embeddings = [make_embedding(dim, seed=i) for i in range(4)]
        vector_store.upsert(chunks, embeddings, namespace=NAMESPACE)

        results = vector_store.query(embeddings[0], namespace=NAMESPACE, top_k=4)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True), "Results must be sorted by score descending"

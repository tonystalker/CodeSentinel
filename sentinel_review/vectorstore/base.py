"""
sentinel_review/vectorstore/base.py
====================================
Shared VectorStore protocol — every backend implements this interface.
The rest of the codebase only imports this; never branches on backend type.
See skill.md section 1.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sentinel_review.ingestion.parser import CodeChunk


class ScoredChunk:
    """A CodeChunk returned from a similarity search, with a relevance score.

    We intentionally avoid inheriting from CodeChunk here to keep base.py
    importable without requiring the full parser dependency chain at import time.
    The score field is appended by each backend after retrieval.
    """

    def __init__(self, chunk: "CodeChunk", score: float) -> None:
        # Copy all chunk fields onto self
        self.__dict__.update(chunk.__dict__)
        self.score = score  # cosine similarity or equivalent, higher = more relevant

    def __repr__(self) -> str:
        return f"ScoredChunk(name={self.name!r}, score={self.score:.4f}, file={self.file_path!r})"


@runtime_checkable
class VectorStore(Protocol):
    """Backend-agnostic vector store interface.

    Implementations: LocalFAISS (vectorstore/local_faiss.py),
                     PineconeStore (vectorstore/pinecone_store.py).
    Selected by: vectorstore/factory.py based on SentinelConfig.vector_store.

    Namespaces isolate repos from each other. One namespace = one repo_id.
    Namespaces persist across runs so incremental indexing has a diff target.
    """

    def upsert(
        self,
        chunks: list["CodeChunk"],
        embeddings: list[list[float]],
        namespace: str,
    ) -> None:
        """Insert or update chunks and their embeddings.

        Args:
            chunks:     CodeChunk objects (with content_hash, metadata etc.)
            embeddings: Parallel list of embedding vectors (same order as chunks).
            namespace:  Repo identifier, e.g. "owner/repo-name".
        """
        ...

    def query(
        self,
        embedding: list[float],
        namespace: str,
        top_k: int = 8,
        filter: dict | None = None,
    ) -> list[ScoredChunk]:
        """Retrieve the top-k most similar chunks.

        Args:
            embedding:  Query vector.
            namespace:  Repo namespace to search within.
            top_k:      Number of results to return.
            filter:     Optional metadata filter (backend-specific semantics).

        Returns:
            List of ScoredChunk, descending by relevance score.
        """
        ...

    def delete_namespace(self, namespace: str) -> None:
        """Delete all chunks and vectors for a namespace.

        Used by the cleanup policy (Pinecone free-tier TTL) and by tests.
        Must be a no-op (not an error) if the namespace doesn't exist.
        """
        ...

    def get_by_chunk_id(self, chunk_id: str, namespace: str) -> "CodeChunk | None":
        """Fetch a single chunk by its stable chunk_id.

        Used by the incremental indexing logic (orchestrator.py) to check
        whether a chunk has changed (compare content_hash) without doing a
        full vector search.

        Returns:
            The CodeChunk if found, None otherwise.
        """
        ...

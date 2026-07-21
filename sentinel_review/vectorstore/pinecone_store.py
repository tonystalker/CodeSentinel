"""
sentinel_review/vectorstore/pinecone_store.py
=============================================
Pinecone vector store backend.

Namespace = repo_id (owner/repo-name). Namespaces persist across runs so
incremental indexing always has something to diff against (skill.md §1).

Requires: PINECONE_API_KEY env var. If absent, factory.py will not
instantiate this class — it falls back to LocalFAISS instead.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel_review.ingestion.parser import CodeChunk
    from sentinel_review.vectorstore.base import ScoredChunk

logger = logging.getLogger(__name__)

_UPSERT_BATCH = 100  # Pinecone upsert is most efficient in batches of ~100


class PineconeStore:
    """Pinecone-backed vector store.

    One Pinecone index, many namespaces (one per repo). This matches the
    free-tier model where you get one index but can segment with namespaces.
    """

    def __init__(self, api_key: str, index_name: str, environment: str) -> None:
        """
        Args:
            api_key:      PINECONE_API_KEY value.
            index_name:   Name of the Pinecone index (must already exist).
            environment:  Pinecone environment string, e.g. "us-east-1-aws".
        """
        try:
            from pinecone import Pinecone
        except ImportError as exc:
            raise ImportError(
                "pinecone-client is not installed. "
                "Run: pip install 'sentinel-review[pinecone]'"
            ) from exc

        pc = Pinecone(api_key=api_key)
        self._index = pc.Index(index_name)
        logger.info("Connected to Pinecone index '%s'", index_name)

    # ------------------------------------------------------------------
    # VectorStore protocol
    # ------------------------------------------------------------------

    def upsert(
        self,
        chunks: list["CodeChunk"],
        embeddings: list[list[float]],
        namespace: str,
    ) -> None:
        """Batch-upsert chunks into the given namespace."""
        if not chunks:
            return

        vectors = []
        for chunk, vec in zip(chunks, embeddings):
            metadata = chunk.to_metadata()
            metadata["code"] = chunk.code  # store code in metadata for retrieval
            vectors.append({
                "id": chunk.chunk_id,
                "values": vec,
                "metadata": metadata,
            })

        # Pinecone recommends batches of ≤100
        for i in range(0, len(vectors), _UPSERT_BATCH):
            batch = vectors[i : i + _UPSERT_BATCH]
            self._index.upsert(vectors=batch, namespace=namespace)

        logger.debug("Upserted %d vectors to namespace '%s'", len(vectors), namespace)

    def query(
        self,
        embedding: list[float],
        namespace: str,
        top_k: int = 8,
        filter: dict | None = None,
    ) -> list["ScoredChunk"]:
        """ANN search within the namespace."""
        from sentinel_review.ingestion.parser import CodeChunk
        from sentinel_review.vectorstore.base import ScoredChunk

        kwargs: dict = {
            "vector": embedding,
            "top_k": top_k,
            "namespace": namespace,
            "include_metadata": True,
        }
        if filter:
            kwargs["filter"] = filter

        response = self._index.query(**kwargs)
        results: list[ScoredChunk] = []

        for match in response.matches:
            m = match.metadata or {}
            code = m.pop("code", "")
            try:
                chunk = CodeChunk.from_metadata(m, code=code)
                results.append(ScoredChunk(chunk, score=match.score))
            except Exception:
                logger.warning("Failed to deserialise chunk '%s' from Pinecone", match.id)
                continue

        return results

    def delete_namespace(self, namespace: str) -> None:
        """Delete all vectors in a namespace. No-op if namespace is empty."""
        try:
            self._index.delete(delete_all=True, namespace=namespace)
            logger.info("Deleted namespace '%s' from Pinecone", namespace)
        except Exception as exc:
            # Pinecone raises if namespace doesn't exist on some SDK versions
            logger.debug("delete_namespace '%s' failed (likely empty): %s", namespace, exc)

    def get_by_chunk_id(self, chunk_id: str, namespace: str) -> "CodeChunk | None":
        """Fetch a single vector by ID. Returns None if not found."""
        from sentinel_review.ingestion.parser import CodeChunk

        try:
            response = self._index.fetch(ids=[chunk_id], namespace=namespace)
        except Exception as exc:
            logger.debug("get_by_chunk_id fetch failed: %s", exc)
            return None

        vectors = response.vectors or {}
        if chunk_id not in vectors:
            return None

        m = dict(vectors[chunk_id].metadata or {})
        code = m.pop("code", "")
        try:
            return CodeChunk.from_metadata(m, code=code)
        except Exception:
            logger.warning("Failed to deserialise chunk '%s' from Pinecone fetch", chunk_id)
            return None

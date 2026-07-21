"""
sentinel_review/ingestion/embedder.py
======================================
Wraps a code-tuned sentence-transformers model for chunk embedding.

Model: jinaai/jina-embeddings-v2-base-code (default in SentinelConfig).
This model produces 768-dim embeddings optimised for code retrieval.

Design decisions:
- Lazy model loading: model is only downloaded/loaded on first call.
  This avoids a 1–2 GB download at import time for users who only want
  to check config or run --help.
- Batching: configurable batch_size (config.embedding_batch_size, default 32)
  to stay within GPU/CPU memory limits.
- Returns raw float lists (not numpy) so results are JSON-serialisable
  and compatible with both FAISS and Pinecone without an extra conversion step.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel_review.ingestion.parser import CodeChunk

logger = logging.getLogger(__name__)


class CodeEmbedder:
    """Lazy-loading wrapper around a sentence-transformers embedding model.

    Usage:
        embedder = CodeEmbedder(model_name="jinaai/jina-embeddings-v2-base-code")
        vectors = embedder.embed(chunks, batch_size=32)
    """

    def __init__(self, model_name: str = "jinaai/jina-embeddings-v2-base-code") -> None:
        self.model_name = model_name
        self._model = None  # loaded on first call to embed()

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is not installed. "
                "Run: pip install sentence-transformers"
            ) from exc

        logger.info("Loading embedding model '%s' (first-time download may take a moment)…", self.model_name)
        self._model = SentenceTransformer(self.model_name, trust_remote_code=True)
        logger.info("Embedding model loaded. Dimension: %d", self._model.get_sentence_embedding_dimension())
        return self._model

    @property
    def embedding_dim(self) -> int:
        """Embedding dimension. Triggers model load if not yet loaded."""
        return self._load_model().get_sentence_embedding_dimension()

    def embed(self, chunks: list["CodeChunk"], batch_size: int = 32) -> list[list[float]]:
        """Embed a list of CodeChunks into float vectors.

        Args:
            chunks:     CodeChunk objects. The ``code`` field is used as
                        the input text. Docstring is prepended if present
                        to give the model more context.
            batch_size: Number of chunks to encode per forward pass.

        Returns:
            List of float vectors (one per chunk, same order).
            Each vector is a list[float] of length self.embedding_dim.
        """
        if not chunks:
            return []

        model = self._load_model()

        # Construct input texts: prepend docstring if available for richer context
        texts = []
        for chunk in chunks:
            if chunk.docstring:
                text = f"{chunk.docstring}\n\n{chunk.code}"
            else:
                text = chunk.code
            texts.append(text)

        logger.debug("Embedding %d chunks in batches of %d", len(texts), batch_size)
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            normalize_embeddings=False,  # we normalise in the vector store query
            convert_to_numpy=True,
        )

        return [vec.tolist() for vec in embeddings]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string (for retrieval, not indexing)."""
        model = self._load_model()
        vec = model.encode(text, normalize_embeddings=False, convert_to_numpy=True)
        return vec.tolist()

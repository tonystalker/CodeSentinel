"""
sentinel_review/vectorstore/factory.py
=======================================
Single entry point for vector store selection. Every call site uses this;
never instantiates LocalFAISS or PineconeStore directly.

Selection is purely config-driven (SentinelConfig.vector_store):
  "local"   → LocalFAISS (always available, zero accounts)
  "pinecone" → PineconeStore (requires PINECONE_API_KEY)

If "pinecone" is selected but no key is available, falls back to "local"
with a warning rather than crashing — consistent with the graceful-degradation
model in skill.md section 0.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sentinel_review.config import SentinelConfig
from sentinel_review.vectorstore.base import VectorStore

logger = logging.getLogger(__name__)


def get_vector_store(
    config: SentinelConfig,
    repo_root: Path | None = None,
    pinecone_api_key: str | None = None,
) -> VectorStore:
    """Return the configured vector store backend.

    Args:
        config:          Loaded SentinelConfig.
        repo_root:       Root directory of the repo being reviewed.
                         Used to resolve the local FAISS cache path.
                         Defaults to the current working directory.
        pinecone_api_key: PINECONE_API_KEY from SentinelSecrets.
                          Required only when config.vector_store == "pinecone".

    Returns:
        An object satisfying the VectorStore protocol.
    """
    repo_root = repo_root or Path.cwd()

    if config.vector_store == "pinecone":
        if not pinecone_api_key:
            logger.warning(
                "vector_store='pinecone' requested but PINECONE_API_KEY is not set. "
                "Falling back to local FAISS. Set PINECONE_API_KEY to use Pinecone."
            )
        else:
            try:
                from sentinel_review.vectorstore.pinecone_store import PineconeStore
                return PineconeStore(
                    api_key=pinecone_api_key,
                    index_name=config.pinecone_index_name,
                    environment=config.pinecone_environment,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to initialise Pinecone (%s). Falling back to local FAISS.", exc
                )

    # Local FAISS — default and fallback
    from sentinel_review.vectorstore.local_faiss import LocalFAISS
    cache_dir = repo_root / config.cache_dir
    logger.debug("Using local FAISS vector store at '%s'", cache_dir)
    return LocalFAISS(cache_root=cache_dir)

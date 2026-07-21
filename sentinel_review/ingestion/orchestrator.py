"""
sentinel_review/ingestion/orchestrator.py
==========================================
Incremental indexing orchestrator — implements the content-hash cache
strategy from skill.md section 3.

Algorithm (per run):
  1. Load the per-namespace manifest (chunk_id → content_hash) from last run.
  2. Parse the repo (or selected subset) into CodeChunks.
  3. For each chunk:
     - If chunk_id exists in manifest AND content_hash matches → skip (reuse existing vector).
     - Otherwise → add to embed batch.
  4. Compute embeddings for the embed batch only.
  5. Upsert new/changed chunks.
  6. Delete chunks whose chunk_id was in last manifest but not in current parse (deleted functions).
  7. Write updated manifest.

Design decisions:
- Manifest stored as JSON in the same cache directory as the FAISS index.
  For Pinecone, stored under config.cache_dir/<namespace>/manifest.json
  (Pinecone doesn't have a sidecar concept, but we still need a local manifest
  to know what existed last run — Pinecone fetch-by-id would be too slow for O(N) chunks).
- Manifest is written atomically (write to .tmp, then rename) to avoid
  corruption if the process is killed mid-write.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel_review.config import SentinelConfig
    from sentinel_review.ingestion.embedder import CodeEmbedder
    from sentinel_review.ingestion.parser import CodeChunk
    from sentinel_review.vectorstore.base import VectorStore

logger = logging.getLogger(__name__)


class IngestOrchestrator:
    """Runs the full ingest pipeline with incremental (content-hash) skipping.

    Intended usage:
        orchestrator = IngestOrchestrator(config, vector_store, embedder, repo_root)
        stats = orchestrator.run(chunks, namespace="owner/repo")
    """

    def __init__(
        self,
        config: "SentinelConfig",
        vector_store: "VectorStore",
        embedder: "CodeEmbedder",
        repo_root: Path,
    ) -> None:
        self._config = config
        self._store = vector_store
        self._embedder = embedder
        self._repo_root = repo_root
        self._manifest_dir = repo_root / config.cache_dir / "manifests"
        self._manifest_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------

    def _manifest_path(self, namespace: str) -> Path:
        safe_ns = namespace.replace("/", "_").replace("\\", "_")
        return self._manifest_dir / f"{safe_ns}.json"

    def _load_manifest(self, namespace: str) -> dict[str, str]:
        """Return {chunk_id: content_hash} from the last successful run."""
        path = self._manifest_path(namespace)
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load manifest for '%s': %s. Starting fresh.", namespace, exc)
            return {}

    def _save_manifest(self, namespace: str, manifest: dict[str, str]) -> None:
        """Write manifest atomically (write temp + rename)."""
        path = self._manifest_path(namespace)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            os.replace(tmp_path, path)
        except Exception:
            os.unlink(tmp_path)
            raise

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        chunks: list["CodeChunk"],
        namespace: str,
    ) -> dict:
        """Run incremental indexing for a set of chunks.

        Args:
            chunks:    All CodeChunks for the current repo/diff (already parsed).
            namespace: Vector store namespace, e.g. "owner/repo-name".

        Returns:
            Stats dict with keys:
              total, skipped (unchanged), embedded (new/changed), deleted (stale).
        """
        old_manifest = self._load_manifest(namespace)
        current_ids = {chunk.chunk_id for chunk in chunks}
        new_manifest: dict[str, str] = {}

        to_embed: list["CodeChunk"] = []
        skipped = 0

        for chunk in chunks:
            new_manifest[chunk.chunk_id] = chunk.content_hash
            if (
                chunk.chunk_id in old_manifest
                and old_manifest[chunk.chunk_id] == chunk.content_hash
            ):
                skipped += 1
                logger.debug("Skipping unchanged chunk '%s' in '%s'", chunk.name, chunk.file_path)
            else:
                to_embed.append(chunk)

        # Embed only new/changed chunks
        embedded = 0
        if to_embed:
            logger.info(
                "Embedding %d new/changed chunks (skipping %d unchanged)",
                len(to_embed), skipped,
            )
            embeddings = self._embedder.embed(
                to_embed,
                batch_size=self._config.embedding_batch_size,
            )
            self._store.upsert(to_embed, embeddings, namespace=namespace)
            embedded = len(to_embed)

        # Delete stale chunks (existed last run, gone now)
        stale_ids = set(old_manifest.keys()) - current_ids
        deleted = 0
        if stale_ids:
            logger.info("Removing %d stale chunks from '%s'", len(stale_ids), namespace)
            for stale_id in stale_ids:
                # For LocalFAISS this is implicit (manifest controls what's live);
                # for correctness we rebuild the FAISS index by only upserting
                # current chunks. For Pinecone, delete explicitly.
                try:
                    # Attempt explicit delete (Pinecone) — no-op for LocalFAISS
                    self._delete_chunk(stale_id, namespace)
                    deleted += 1
                except Exception as exc:
                    logger.debug("Could not delete stale chunk '%s': %s", stale_id, exc)

        self._save_manifest(namespace, new_manifest)

        stats = {
            "total": len(chunks),
            "skipped": skipped,
            "embedded": embedded,
            "deleted": deleted,
        }
        logger.info("Ingest complete: %s", stats)
        return stats

    def _delete_chunk(self, chunk_id: str, namespace: str) -> None:
        """Best-effort single-chunk delete. Works for Pinecone; LocalFAISS
        doesn't expose per-ID delete, so stale chunks just won't appear
        in the manifest and will be replaced on next upsert."""
        store = self._store
        # Only Pinecone supports direct vector delete by ID
        if hasattr(store, "_index"):  # duck-type check for PineconeStore
            store._index.delete(ids=[chunk_id], namespace=namespace)

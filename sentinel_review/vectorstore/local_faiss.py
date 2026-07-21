"""
sentinel_review/vectorstore/local_faiss.py
==========================================
Local FAISS vector store backend — zero external accounts needed.

Persists to ``<repo_root>/<config.cache_dir>/<repo_id>/``:
  - ``index.faiss``  — FAISS flat inner-product index
  - ``metadata.json`` — chunk_id → {code, ...metadata} mapping
  - ``vectors.npy``   — numpy array of stored vectors (parallel to metadata)

Namespace = repo_id (sanitised to a valid directory name).
See skill.md section 1 for the interface contract.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentinel_review.ingestion.parser import CodeChunk
    from sentinel_review.vectorstore.base import ScoredChunk


def _sanitise_namespace(namespace: str) -> str:
    """Convert 'owner/repo-name' to a safe directory component."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", namespace)


class LocalFAISS:
    """FAISS-backed vector store for local, offline use.

    Thread safety: NOT thread-safe. Designed for single-process CLI usage.
    For concurrent access, use Pinecone.
    """

    def __init__(self, cache_root: Path) -> None:
        """
        Args:
            cache_root: Root cache directory, e.g. Path(".sentinel_cache").
                        Namespace subdirectories are created underneath.
        """
        self._cache_root = cache_root

    # ------------------------------------------------------------------
    # Namespace directory helpers
    # ------------------------------------------------------------------

    def _ns_dir(self, namespace: str) -> Path:
        return self._cache_root / _sanitise_namespace(namespace)

    def _index_path(self, namespace: str) -> Path:
        return self._ns_dir(namespace) / "index.faiss"

    def _meta_path(self, namespace: str) -> Path:
        return self._ns_dir(namespace) / "metadata.json"

    def _vec_path(self, namespace: str) -> Path:
        return self._ns_dir(namespace) / "vectors.npy"

    # ------------------------------------------------------------------
    # Load / save helpers
    # ------------------------------------------------------------------

    def _load(self, namespace: str) -> tuple[dict, np.ndarray]:
        """Load metadata dict and vector matrix for a namespace.

        Returns:
            (metadata_dict, vectors_array)
            metadata_dict: chunk_id → {code, ...metadata fields}
            vectors_array: shape (N, dim) float32, rows align with metadata order
        """
        import faiss  # lazy import so non-FAISS users don't pay the cost

        meta_path = self._meta_path(namespace)
        vec_path = self._vec_path(namespace)

        if not meta_path.exists():
            return {}, np.empty((0, 0), dtype=np.float32)

        with meta_path.open("r", encoding="utf-8") as fh:
            meta = json.load(fh)

        if vec_path.exists():
            vecs = np.load(str(vec_path))
        else:
            vecs = np.empty((0, 0), dtype=np.float32)

        return meta, vecs

    def _save(self, namespace: str, meta: dict, vecs: np.ndarray) -> None:
        ns_dir = self._ns_dir(namespace)
        ns_dir.mkdir(parents=True, exist_ok=True)

        with self._meta_path(namespace).open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

        if vecs.size > 0:
            np.save(str(self._vec_path(namespace)), vecs)

    # ------------------------------------------------------------------
    # VectorStore protocol
    # ------------------------------------------------------------------

    def upsert(
        self,
        chunks: list["CodeChunk"],
        embeddings: list[list[float]],
        namespace: str,
    ) -> None:
        """Insert or replace chunks. Existing chunk_ids are overwritten."""
        if not chunks:
            return

        meta, existing_vecs = self._load(namespace)

        # Build updated metadata and vector arrays
        new_vecs = np.array(embeddings, dtype=np.float32)

        for i, chunk in enumerate(chunks):
            meta[chunk.chunk_id] = {
                "code": chunk.code,
                **chunk.to_metadata(),
                "_vec_idx": None,  # placeholder; we'll recompute after sort
            }

        # Rebuild ordered lists from meta dict (keep existing + new/updated)
        # We store vectors in insertion order; chunk_id → row index via _vec_idx
        # Strategy: collect all chunk_ids in stable order, rebuild full matrix

        # Collect IDs that already had vectors
        existing_ids = [
            cid for cid, m in meta.items() if m.get("_vec_idx") is not None
        ]
        new_ids = [c.chunk_id for c in chunks]

        # Merge: keep existing not in new batch + add/update new
        all_ids_ordered: list[str] = []
        seen: set[str] = set(new_ids)
        for cid in existing_ids:
            if cid not in seen:
                all_ids_ordered.append(cid)
        all_ids_ordered.extend(new_ids)

        # Rebuild full vector matrix
        dim = new_vecs.shape[1]
        full_vecs_list: list[np.ndarray] = []

        for cid in all_ids_ordered:
            if cid in set(new_ids):
                idx_in_new = new_ids.index(cid)
                full_vecs_list.append(new_vecs[idx_in_new])
            else:
                old_idx = meta[cid]["_vec_idx"]
                full_vecs_list.append(existing_vecs[old_idx])

        full_vecs = np.stack(full_vecs_list, axis=0).astype(np.float32) if full_vecs_list else np.empty((0, dim), dtype=np.float32)

        # Update _vec_idx in metadata
        for row_idx, cid in enumerate(all_ids_ordered):
            meta[cid]["_vec_idx"] = row_idx

        self._save(namespace, meta, full_vecs)

    def query(
        self,
        embedding: list[float],
        namespace: str,
        top_k: int = 8,
        filter: dict | None = None,
    ) -> list["ScoredChunk"]:
        """Cosine-similarity search over the namespace.

        ``filter`` is applied post-search (FAISS doesn't support metadata
        filtering natively). Keys in filter are AND-ed as equality checks
        against chunk metadata fields.
        """
        from sentinel_review.ingestion.parser import CodeChunk
        from sentinel_review.vectorstore.base import ScoredChunk

        meta, vecs = self._load(namespace)
        if vecs.size == 0 or not meta:
            return []

        query_vec = np.array(embedding, dtype=np.float32)
        # Normalise for cosine similarity
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
        vecs_norm = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-10)
        scores = vecs_norm @ query_norm  # shape (N,)

        # Sort descending
        ranked_indices = np.argsort(-scores)

        # Build id lookup by vec index
        idx_to_cid = {m["_vec_idx"]: cid for cid, m in meta.items()}

        results: list["ScoredChunk"] = []
        for row_idx in ranked_indices:
            if len(results) >= top_k:
                break
            cid = idx_to_cid.get(int(row_idx))
            if cid is None:
                continue
            m = meta[cid]

            # Apply metadata filter
            if filter:
                if not all(m.get(k) == v for k, v in filter.items()):
                    continue

            chunk = CodeChunk.from_metadata(
                {k: v for k, v in m.items() if k not in ("code", "_vec_idx")},
                code=m["code"],
            )
            results.append(ScoredChunk(chunk, float(scores[row_idx])))

        return results

    def delete_namespace(self, namespace: str) -> None:
        """Remove all data for a namespace. No-op if it doesn't exist."""
        import shutil
        ns_dir = self._ns_dir(namespace)
        if ns_dir.exists():
            shutil.rmtree(ns_dir)

    def get_by_chunk_id(self, chunk_id: str, namespace: str) -> "CodeChunk | None":
        """Fetch a single chunk by chunk_id without a vector search."""
        from sentinel_review.ingestion.parser import CodeChunk

        meta, _ = self._load(namespace)
        m = meta.get(chunk_id)
        if m is None:
            return None

        return CodeChunk.from_metadata(
            {k: v for k, v in m.items() if k not in ("code", "_vec_idx")},
            code=m["code"],
        )

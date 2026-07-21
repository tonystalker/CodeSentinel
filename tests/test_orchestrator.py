"""
tests/test_orchestrator.py
============================
Task 3 acceptance tests for IngestOrchestrator.

Done-when criteria (build.md Task 3):
  ✓ Running orchestrator twice on an unchanged repo → zero embedding calls second time
  ✓ Changing one function → only that chunk is re-embedded
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel_review.config import SentinelConfig
from sentinel_review.ingestion.embedder import CodeEmbedder
from sentinel_review.ingestion.orchestrator import IngestOrchestrator
from sentinel_review.ingestion.parser import CodeChunk
from sentinel_review.vectorstore.local_faiss import LocalFAISS


def make_chunk(name: str, code: str = "def foo(): pass") -> CodeChunk:
    return CodeChunk.build(
        file_path="test.py",
        name=name,
        kind="function",
        start_line=1,
        end_line=3,
        code=code,
    )


@pytest.fixture()
def setup(tmp_path: Path):
    config = SentinelConfig()
    store = LocalFAISS(cache_root=tmp_path / ".sentinel_cache")
    # Mock embedder — we don't want to download the model in unit tests
    embedder = MagicMock(spec=CodeEmbedder)
    embedder.embed.return_value = [[0.1] * 8]  # 1 vector per call
    return config, store, embedder, tmp_path


class TestIngestOrchestrator:
    def test_unchanged_repo_skips_embedding_on_second_run(self, setup, tmp_path: Path) -> None:
        """Running orchestrator twice on unchanged repo → zero embedding calls second time."""
        config, store, embedder, repo_root = setup
        orchestrator = IngestOrchestrator(config, store, embedder, repo_root)

        chunks = [make_chunk("alpha"), make_chunk("beta")]
        embedder.embed.return_value = [[0.1] * 8, [0.2] * 8]

        # First run — embeds both
        stats1 = orchestrator.run(chunks, namespace="test/repo")
        assert stats1["embedded"] == 2
        assert stats1["skipped"] == 0

        # Reset mock call count
        embedder.embed.reset_mock()
        embedder.embed.return_value = [[0.1] * 8, [0.2] * 8]

        # Second run — same chunks, nothing changed
        stats2 = orchestrator.run(chunks, namespace="test/repo")
        assert stats2["skipped"] == 2
        assert stats2["embedded"] == 0
        embedder.embed.assert_not_called()  # Key assertion: no embedding calls

    def test_changed_chunk_triggers_re_embed(self, setup, tmp_path: Path) -> None:
        """Changing one function's code → only that chunk is re-embedded."""
        config, store, embedder, repo_root = setup
        orchestrator = IngestOrchestrator(config, store, embedder, repo_root)

        chunk_alpha = make_chunk("alpha", code="def alpha(): return 1")
        chunk_beta = make_chunk("beta", code="def beta(): return 2")
        embedder.embed.return_value = [[0.1] * 8, [0.2] * 8]

        # First run
        orchestrator.run([chunk_alpha, chunk_beta], namespace="test/repo")
        embedder.embed.reset_mock()

        # Change only beta
        chunk_beta_v2 = make_chunk("beta", code="def beta(): return 999")
        assert chunk_beta_v2.content_hash != chunk_beta.content_hash  # Sanity check

        embedder.embed.return_value = [[0.9] * 8]

        stats = orchestrator.run([chunk_alpha, chunk_beta_v2], namespace="test/repo")
        assert stats["skipped"] == 1   # alpha unchanged
        assert stats["embedded"] == 1  # beta changed

        # Verify embed was called with only the changed chunk
        call_args = embedder.embed.call_args
        embedded_chunks = call_args[0][0]
        assert len(embedded_chunks) == 1
        assert embedded_chunks[0].name == "beta"

    def test_deleted_chunk_tracked_in_stats(self, setup, tmp_path: Path) -> None:
        """Chunk present in first run but absent in second → deleted count > 0."""
        config, store, embedder, repo_root = setup
        orchestrator = IngestOrchestrator(config, store, embedder, repo_root)

        chunks = [make_chunk("alpha"), make_chunk("to_delete")]
        embedder.embed.return_value = [[0.1] * 8, [0.2] * 8]
        orchestrator.run(chunks, namespace="test/repo")
        embedder.embed.reset_mock()

        # Second run: to_delete is gone
        embedder.embed.return_value = []
        stats = orchestrator.run([make_chunk("alpha")], namespace="test/repo")
        assert stats["deleted"] >= 1  # to_delete should be tracked as stale

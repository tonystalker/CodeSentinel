"""
tests/test_parser.py
======================
Tests for the Tree-sitter AST parser.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sentinel_review.ingestion.parser import CodeChunk, parse_file, parse_repo


@pytest.fixture()
def sample_py(tmp_path: Path) -> Path:
    """Write a small Python file with functions and classes."""
    code = '''\
import json


def load_settings(raw: str) -> dict:
    """Load settings from JSON."""
    return json.loads(raw)


def helper(x):
    return x * 2


class App:
    def __init__(self):
        self.name = "test"

    def run(self):
        return self.name
'''
    f = tmp_path / "sample.py"
    f.write_text(code, encoding="utf-8")
    return f


class TestParseFile:
    def test_returns_chunks(self, sample_py: Path, tmp_path: Path) -> None:
        chunks = parse_file(sample_py, tmp_path)
        assert len(chunks) >= 1

    def test_chunk_has_content_hash(self, sample_py: Path, tmp_path: Path) -> None:
        chunks = parse_file(sample_py, tmp_path)
        for chunk in chunks:
            assert chunk.content_hash
            assert len(chunk.content_hash) == 40  # sha1 hex

    def test_chunk_id_stable(self, sample_py: Path, tmp_path: Path) -> None:
        """Same file parsed twice → same chunk_ids."""
        chunks1 = parse_file(sample_py, tmp_path)
        chunks2 = parse_file(sample_py, tmp_path)
        ids1 = {c.chunk_id for c in chunks1}
        ids2 = {c.chunk_id for c in chunks2}
        assert ids1 == ids2

    def test_chunk_code_field_is_populated(self, sample_py: Path, tmp_path: Path) -> None:
        chunks = parse_file(sample_py, tmp_path)
        for chunk in chunks:
            assert chunk.code.strip()

    def test_file_path_is_relative(self, sample_py: Path, tmp_path: Path) -> None:
        chunks = parse_file(sample_py, tmp_path)
        for chunk in chunks:
            assert not chunk.file_path.startswith("/")
            assert not chunk.file_path.startswith("C:")

    def test_unsupported_extension_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "file.md"
        f.write_text("# Hello")
        chunks = parse_file(f, tmp_path)
        assert chunks == []

    def test_content_hash_changes_when_code_changes(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    return 1\n")
        chunks_v1 = parse_file(f, tmp_path)

        f.write_text("def foo():\n    return 2\n")
        chunks_v2 = parse_file(f, tmp_path)

        if chunks_v1 and chunks_v2:
            assert chunks_v1[0].content_hash != chunks_v2[0].content_hash


class TestParseRepo:
    def test_parse_repo_finds_python_files(self, sample_py: Path, tmp_path: Path) -> None:
        chunks = parse_repo(tmp_path)
        assert len(chunks) >= 1

    def test_parse_repo_respects_ignore_patterns(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("def foo(): pass")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("def test_foo(): pass")

        chunks = parse_repo(tmp_path, ignore_patterns=["tests/*"])
        file_paths = {c.file_path for c in chunks}
        assert not any("tests/" in p for p in file_paths)

    def test_to_metadata_roundtrip(self, sample_py: Path, tmp_path: Path) -> None:
        chunks = parse_file(sample_py, tmp_path)
        for chunk in chunks:
            meta = chunk.to_metadata()
            reconstructed = CodeChunk.from_metadata(meta, code=chunk.code)
            assert reconstructed.chunk_id == chunk.chunk_id
            assert reconstructed.content_hash == chunk.content_hash
            assert reconstructed.name == chunk.name

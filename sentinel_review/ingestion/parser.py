"""
sentinel_review/ingestion/parser.py
=====================================
Tree-sitter AST parser — extracts function/class chunks from source files.

Produces CodeChunk objects with:
  - stable chunk_id (file_path + name + start_line)
  - content_hash (sha1 of code text) for incremental indexing
  - call graph (naive: function names appearing in the body)
  - metadata suitable for vector store upsert

Gotcha (skill.md): tree-sitter-languages is broken on current tree-sitter.
Use per-language packages (tree-sitter-python, tree-sitter-javascript, etc.)
directly. Never add tree-sitter-languages as a dependency.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Language bindings — per-language packages, NOT tree-sitter-languages
try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    PY_LANGUAGE = Language(tspython.language())
except Exception:
    PY_LANGUAGE = None  # type: ignore[assignment]

try:
    import tree_sitter_javascript as tsjs

    JS_LANGUAGE = Language(tsjs.language())
except Exception:
    JS_LANGUAGE = None  # type: ignore[assignment]

try:
    import tree_sitter_typescript as tsts

    TS_LANGUAGE = Language(tsts.language_typescript())
    TSX_LANGUAGE = Language(tsts.language_tsx())
except Exception:
    TS_LANGUAGE = None  # type: ignore[assignment]
    TSX_LANGUAGE = None  # type: ignore[assignment]

try:
    import tree_sitter_java as tsjava

    JAVA_LANGUAGE = Language(tsjava.language())
except Exception:
    JAVA_LANGUAGE = None  # type: ignore[assignment]

try:
    import tree_sitter_go as tsgo

    GO_LANGUAGE = Language(tsgo.language())
except Exception:
    GO_LANGUAGE = None  # type: ignore[assignment]

# Extension → tree-sitter Language object
_LANG_MAP: dict[str, object] = {}
if PY_LANGUAGE:
    _LANG_MAP.update({".py": PY_LANGUAGE})
if JS_LANGUAGE:
    _LANG_MAP.update({".js": JS_LANGUAGE, ".jsx": JS_LANGUAGE})
if TS_LANGUAGE:
    _LANG_MAP.update({".ts": TS_LANGUAGE})
if TSX_LANGUAGE:
    _LANG_MAP.update({".tsx": TSX_LANGUAGE})
if JAVA_LANGUAGE:
    _LANG_MAP.update({".java": JAVA_LANGUAGE})
if GO_LANGUAGE:
    _LANG_MAP.update({".go": GO_LANGUAGE})

SUPPORTED_EXTENSIONS = set(_LANG_MAP.keys())

# Node types that represent "a named callable / class" across languages
_CHUNK_NODE_TYPES = {
    "function_definition",       # Python
    "async_function_definition", # Python async def
    "class_definition",          # Python
    "function_declaration",      # JS/TS/Java/Go
    "method_definition",         # JS/TS class methods
    "arrow_function",            # JS/TS — only named ones (assigned to const)
    "class_declaration",         # JS/TS/Java
    "method_declaration",        # Java
    "func_declaration",          # Go
    "function_literal",          # Go func literals in vars
}

ChunkKind = Literal["function", "class", "method"]


@dataclass
class CodeChunk:
    """A single reviewable unit — one function, method, or class.

    Attributes:
        chunk_id:     Stable identifier: sha1(file_path + "::" + name + "::" + str(start_line))
        file_path:    Relative path from repo root.
        name:         Function/class/method name, or "<anonymous>" for unnamed.
        kind:         "function", "class", or "method".
        start_line:   1-indexed line where the chunk begins.
        end_line:     1-indexed line where the chunk ends (inclusive).
        code:         Full source text of the chunk.
        content_hash: sha1 of `code` — used by incremental indexing to skip
                      unchanged chunks (skill.md §3).
        docstring:    Extracted docstring / leading comment, or "".
        calls:        Set of function/method names called within the body.
                      Used by diff_selector for 1-hop call-graph expansion (skill.md §4).
        language:     Source language (e.g. "python", "javascript").
    """

    chunk_id: str
    file_path: str
    name: str
    kind: ChunkKind
    start_line: int
    end_line: int
    code: str
    content_hash: str
    docstring: str = ""
    calls: set[str] = field(default_factory=set)
    language: str = "unknown"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        file_path: str,
        name: str,
        kind: ChunkKind,
        start_line: int,
        end_line: int,
        code: str,
        docstring: str = "",
        calls: set[str] | None = None,
        language: str = "unknown",
    ) -> "CodeChunk":
        """Construct a CodeChunk, computing chunk_id and content_hash."""
        chunk_id = hashlib.sha1(
            f"{file_path}::{name}::{start_line}".encode()
        ).hexdigest()
        content_hash = hashlib.sha1(code.encode()).hexdigest()
        return cls(
            chunk_id=chunk_id,
            file_path=file_path,
            name=name,
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            code=code,
            content_hash=content_hash,
            docstring=docstring,
            calls=calls or set(),
            language=language,
        )

    # ------------------------------------------------------------------
    # Serialisation helpers (for vector store metadata sidecars)
    # ------------------------------------------------------------------

    def to_metadata(self) -> dict:
        """Convert to a flat dict suitable for vector store metadata fields.

        Lists/sets are JSON-serialisable. Avoids nesting to stay compatible
        with Pinecone metadata limits.
        """
        return {
            "chunk_id": self.chunk_id,
            "file_path": self.file_path,
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content_hash": self.content_hash,
            "docstring": self.docstring,
            "calls": list(self.calls),
            "language": self.language,
        }

    @classmethod
    def from_metadata(cls, metadata: dict, code: str) -> "CodeChunk":
        """Reconstruct a CodeChunk from a metadata dict + the stored code text."""
        return cls(
            chunk_id=metadata["chunk_id"],
            file_path=metadata["file_path"],
            name=metadata["name"],
            kind=metadata["kind"],
            start_line=metadata["start_line"],
            end_line=metadata["end_line"],
            code=code,
            content_hash=metadata["content_hash"],
            docstring=metadata.get("docstring", ""),
            calls=set(metadata.get("calls", [])),
            language=metadata.get("language", "unknown"),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_node_name(node, source_bytes: bytes) -> str:
    """Extract the identifier name from a function/class declaration node."""
    for child in node.children:
        if child.type == "identifier":
            return source_bytes[child.start_byte:child.end_byte].decode(errors="replace")
    return "<anonymous>"


def _extract_docstring(node, source_bytes: bytes, language: str) -> str:
    """Extract the leading docstring or block comment from a chunk node."""
    body_node = None
    for child in node.children:
        if child.type in ("block", "statement_block", "body"):
            body_node = child
            break

    if body_node is None:
        return ""

    for child in body_node.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = source_bytes[sub.start_byte:sub.end_byte].decode(errors="replace")
                    return raw.strip('"\' \t\n').strip()
        elif child.type == "comment":
            return source_bytes[child.start_byte:child.end_byte].decode(errors="replace").lstrip("/#* \t")
        elif child.type not in ("comment", "\n", "newline"):
            break

    return ""


def _extract_calls(node, source_bytes: bytes) -> set[str]:
    """Walk the subtree collecting all call expression identifiers."""
    calls: set[str] = set()

    def walk(n) -> None:
        if n.type == "call":
            # function call: first child is usually the identifier or attribute
            func_node = n.child_by_field_name("function") or (n.children[0] if n.children else None)
            if func_node:
                if func_node.type == "identifier":
                    calls.add(source_bytes[func_node.start_byte:func_node.end_byte].decode(errors="replace"))
                elif func_node.type == "attribute":
                    # e.g. obj.method() — record just the method name
                    attr = func_node.child_by_field_name("attribute")
                    if attr:
                        calls.add(source_bytes[attr.start_byte:attr.end_byte].decode(errors="replace"))
        for child in n.children:
            walk(child)

    walk(node)
    return calls


def _infer_kind(node_type: str) -> ChunkKind:
    if "class" in node_type:
        return "class"
    if "method" in node_type:
        return "method"
    return "function"


def _infer_language(ext: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
    }.get(ext, "unknown")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(file_path: Path, repo_root: Path) -> list[CodeChunk]:
    """Parse a single source file into CodeChunk objects.

    Args:
        file_path: Absolute path to the file.
        repo_root: Repo root — used to compute relative path for chunk metadata.

    Returns:
        List of CodeChunk, one per top-level function/class. Empty list if the
        file's extension is unsupported or the file cannot be parsed.
    """
    ext = file_path.suffix.lower()
    language_obj = _LANG_MAP.get(ext)
    if language_obj is None:
        return []

    try:
        source_bytes = file_path.read_bytes()
    except OSError:
        return []

    try:
        parser = Parser(language_obj)  # type: ignore[arg-type]
        tree = parser.parse(source_bytes)
    except Exception:
        return []

    rel_path = str(file_path.relative_to(repo_root)).replace("\\", "/")
    lang_name = _infer_language(ext)
    chunks: list[CodeChunk] = []

    def visit(node) -> None:
        if node.type in _CHUNK_NODE_TYPES:
            name = _get_node_name(node, source_bytes)
            start_line = node.start_point[0] + 1  # tree-sitter is 0-indexed
            end_line = node.end_point[0] + 1
            code = source_bytes[node.start_byte:node.end_byte].decode(errors="replace")
            docstring = _extract_docstring(node, source_bytes, lang_name)
            calls = _extract_calls(node, source_bytes)
            kind = _infer_kind(node.type)

            chunks.append(
                CodeChunk.build(
                    file_path=rel_path,
                    name=name,
                    kind=kind,
                    start_line=start_line,
                    end_line=end_line,
                    code=code,
                    docstring=docstring,
                    calls=calls,
                    language=lang_name,
                )
            )
            # Don't recurse into nested functions (they'll create their own chunks)
            # Exception: class bodies — we want methods chunked separately too
            if "class" not in node.type:
                return

        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return chunks


def parse_repo(repo_root: Path, ignore_patterns: list[str] | None = None) -> list[CodeChunk]:
    """Recursively parse all supported files in a repository.

    Args:
        repo_root:       Root directory of the repository.
        ignore_patterns: Glob patterns to skip (e.g. ["tests/*", ".venv/*"]).

    Returns:
        Flat list of CodeChunk across all parsed files.
    """
    import fnmatch

    ignore_patterns = ignore_patterns or []
    all_chunks: list[CodeChunk] = []

    for file_path in sorted(repo_root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        rel = str(file_path.relative_to(repo_root)).replace("\\", "/")
        if any(fnmatch.fnmatch(rel, pat) for pat in ignore_patterns):
            continue

        all_chunks.extend(parse_file(file_path, repo_root))

    return all_chunks

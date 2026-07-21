"""
sentinel_review/ingestion/diff_selector.py
==========================================
Diff-aware chunk selection — implements skill.md section 4.

Algorithm:
  1. Run git diff base_ref...head_ref --unified=0 via GitPython → changed line ranges per file.
  2. Parse each changed file with parser.py → chunks.
  3. Keep chunks whose [start_line, end_line] overlaps any changed range.
  4. Expand: pull each kept chunk's `calls` targets from the all_chunks set,
     fetch those chunks too (1-hop only — don't recursively expand).
  5. Return the expanded set.

Full-repo scan is the same pipeline with step 1 skipped (all chunks are
"changed"). Only one code path: diff=None means all chunks are selected.

Design decision: We operate on the already-parsed all_chunks list rather
than re-parsing. This means the caller must parse the full repo before
calling get_changed_chunks — which is fine because the incremental indexer
will skip unchanged chunks anyway.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel_review.ingestion.parser import CodeChunk

logger = logging.getLogger(__name__)

# A changed range: (start_line_1indexed, end_line_1indexed)
ChangedRange = tuple[int, int]


def _get_git_changed_ranges(
    repo_path: Path,
    base_ref: str,
    head_ref: str,
) -> dict[str, list[ChangedRange]]:
    """Run git diff and return per-file line ranges that changed.

    Returns:
        {relative_file_path: [(start_line, end_line), ...]}

    Empty dict if git is not available or not a git repo.
    """
    try:
        import git
    except ImportError:
        logger.warning("gitpython not installed — treating as full repo scan")
        return {}

    try:
        repo = git.Repo(repo_path, search_parent_directories=True)
    except git.InvalidGitRepositoryError:
        logger.warning("'%s' is not a git repository — treating as full scan", repo_path)
        return {}

    changed: dict[str, list[ChangedRange]] = {}

    try:
        # unified=0 gives us the hunk headers with exact changed ranges, no context
        diff_output = repo.git.diff(f"{base_ref}...{head_ref}", "--unified=0", "--name-only=false")
    except git.GitCommandError as exc:
        logger.warning("git diff failed (%s) — falling back to full repo scan", exc)
        return {}

    # Parse diff output: look for @@ -a,b +c,d @@ lines
    import re
    current_file: str | None = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    file_re = re.compile(r"^\+\+\+ b/(.+)$")

    for line in diff_output.splitlines():
        fm = file_re.match(line)
        if fm:
            current_file = fm.group(1).replace("\\", "/")
            changed.setdefault(current_file, [])
            continue
        hm = hunk_re.match(line)
        if hm and current_file:
            start = int(hm.group(1))
            count = int(hm.group(2)) if hm.group(2) is not None else 1
            end = start + max(count - 1, 0)
            changed[current_file].append((start, end))

    return changed


def _overlaps(chunk_start: int, chunk_end: int, ranges: list[ChangedRange]) -> bool:
    """Return True if [chunk_start, chunk_end] overlaps any range in ranges."""
    for (rstart, rend) in ranges:
        if chunk_start <= rend and chunk_end >= rstart:
            return True
    return False


def get_changed_chunks(
    repo_path: Path,
    base_ref: str | None,
    head_ref: str | None,
    all_chunks: list["CodeChunk"],
) -> list["CodeChunk"]:
    """Return the subset of chunks to analyse for this run.

    Args:
        repo_path:  Local path to the git repo root.
        base_ref:   Git ref to diff against (e.g. "main"). None → full scan.
        head_ref:   Git ref for the current state (e.g. "HEAD"). None → full scan.
        all_chunks: All chunks parsed from the repo (full parse required for call-graph expansion).

    Returns:
        Expanded list of CodeChunks to pass to the agent workflow.
        For full scan (base_ref=None), returns all_chunks unchanged.
    """
    # Full-repo scan: same code path, all chunks selected
    if base_ref is None or head_ref is None:
        logger.info("Full repo scan: %d chunks selected", len(all_chunks))
        return all_chunks

    # Step 1: get changed line ranges from git
    changed_ranges = _get_git_changed_ranges(repo_path, base_ref, head_ref)

    if not changed_ranges:
        # git diff failed or empty diff → treat as full scan
        logger.info("No diff data — treating as full repo scan")
        return all_chunks

    # Step 2 & 3: filter chunks by overlap with changed ranges
    direct_chunks: list["CodeChunk"] = []
    for chunk in all_chunks:
        file_ranges = changed_ranges.get(chunk.file_path, [])
        if file_ranges and _overlaps(chunk.start_line, chunk.end_line, file_ranges):
            direct_chunks.append(chunk)

    logger.info(
        "Diff-aware: %d chunks directly changed (from %d total)",
        len(direct_chunks), len(all_chunks),
    )

    # Step 4: 1-hop call-graph expansion
    # Build name → chunk lookup for fast resolution
    name_to_chunks: dict[str, list["CodeChunk"]] = {}
    for chunk in all_chunks:
        name_to_chunks.setdefault(chunk.name, []).append(chunk)

    expanded_ids: set[str] = {c.chunk_id for c in direct_chunks}
    expanded: list["CodeChunk"] = list(direct_chunks)

    for chunk in direct_chunks:
        for called_name in chunk.calls:
            for callee_chunk in name_to_chunks.get(called_name, []):
                if callee_chunk.chunk_id not in expanded_ids:
                    expanded.append(callee_chunk)
                    expanded_ids.add(callee_chunk.chunk_id)
                    logger.debug(
                        "Call-graph expansion: added '%s' (called from '%s')",
                        callee_chunk.name, chunk.name,
                    )

    logger.info(
        "After 1-hop expansion: %d chunks selected for analysis",
        len(expanded),
    )
    return expanded

"""
sentinel_review/ingestion/github_loader.py
==========================================
Load a GitHub repository by shallow-cloning it to a temp directory.

Returns:
    (local_path: Path, repo_id: str)

repo_id format: "owner/repo-name" — used as the vector store namespace.

Design decisions:
- depth=1 clone: we don't need full history for analysis, and it's much faster.
- Temp directory is cleaned up by the caller (or by the OS on process exit).
  We return the path so the caller controls lifecycle (important for E2B integration).
- Accepts both HTTPS and SSH URLs; normalises to HTTPS for unauthenticated clones.
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_GITHUB_PATTERNS = [
    # https://github.com/owner/repo or https://github.com/owner/repo.git
    re.compile(r"https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?"),
    # git@github.com:owner/repo.git
    re.compile(r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?"),
]


def is_github_url(url: str) -> bool:
    return any(p.match(url) for p in _GITHUB_PATTERNS)


def parse_repo_id(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL."""
    for pattern in _GITHUB_PATTERNS:
        m = pattern.match(url)
        if m:
            return f"{m.group('owner')}/{m.group('repo')}"
    raise ValueError(f"Cannot parse GitHub URL: {url!r}")


def clone_github_repo(url: str, target_dir: Path | None = None) -> tuple[Path, str]:
    """Shallow-clone a GitHub repository.

    Args:
        url:        GitHub repository URL (HTTPS or SSH).
        target_dir: Where to clone. If None, a temp directory is created.

    Returns:
        (local_path, repo_id) where repo_id is "owner/repo".

    Raises:
        ImportError:  If gitpython is not installed.
        git.GitCommandError: If the clone fails (private repo, bad URL, etc.).
    """
    try:
        import git
    except ImportError as exc:
        raise ImportError(
            "gitpython is not installed. Run: pip install gitpython"
        ) from exc

    repo_id = parse_repo_id(url)

    if target_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="sentinel_"))

    target_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning '%s' (depth=1) to '%s'…", repo_id, target_dir)

    git.Repo.clone_from(url, str(target_dir), depth=1, no_single_branch=True)

    logger.info("Clone complete: '%s'", repo_id)
    return target_dir, repo_id

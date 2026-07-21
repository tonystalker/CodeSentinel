"""
sentinel_review/ingestion/zip_loader.py
=========================================
Extract a ZIP archive into a temp directory with zip-slip protection.

Zip-slip: a malicious ZIP can contain entries with ``../`` paths that would
write files outside the target directory. We validate every extracted path
before writing.

Returns:
    (local_path: Path, repo_id: str)

repo_id is derived from the ZIP filename (without extension).
"""
from __future__ import annotations

import logging
import tempfile
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_zip(zip_path: Path, target_dir: Path | None = None) -> tuple[Path, str]:
    """Extract a ZIP archive with zip-slip protection.

    Args:
        zip_path:   Path to the .zip file.
        target_dir: Where to extract. If None, a temp directory is created.

    Returns:
        (local_path, repo_id) — local_path is the extraction root,
        repo_id is derived from the ZIP filename stem.

    Raises:
        ValueError: If any entry in the ZIP would write outside target_dir
                    (zip-slip attack attempt).
        zipfile.BadZipFile: If the file is not a valid ZIP archive.
    """
    if target_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="sentinel_zip_"))

    target_dir = target_dir.resolve()
    repo_id = zip_path.stem  # e.g. "myproject-main" → repo_id = "myproject-main"

    logger.info("Extracting '%s' to '%s'…", zip_path.name, target_dir)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            # Resolve the full target path
            target_member = (target_dir / member.filename).resolve()

            # Zip-slip check: must stay under target_dir
            if not str(target_member).startswith(str(target_dir)):
                raise ValueError(
                    f"Zip-slip detected: '{member.filename}' would extract to "
                    f"'{target_member}' which is outside target '{target_dir}'. "
                    "Refusing to extract potentially malicious archive."
                )

            # Extract safely
            if member.is_dir():
                target_member.mkdir(parents=True, exist_ok=True)
            else:
                target_member.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target_member.open("wb") as dst:
                    dst.write(src.read())

    # Many GitHub ZIPs extract to a subdirectory named "<repo>-<branch>/"
    # Detect single top-level directory and use it as the actual root
    entries = list(target_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        actual_root = entries[0]
        logger.debug("Single top-level dir detected: using '%s' as repo root", actual_root.name)
    else:
        actual_root = target_dir

    logger.info("Extraction complete: %d entries", len(list(actual_root.rglob("*"))))
    return actual_root, repo_id

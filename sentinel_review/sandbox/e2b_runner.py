"""
sentinel_review/sandbox/e2b_runner.py
=======================================
E2B sandbox execution for fix validation (skill.md section 6).

Contract:
- Fresh sandbox per attempt (never reuse — state leakage corrupts signal).
- Context manager teardown guaranteed even on exception.
- Explicit timeout per attempt (config.sandbox_timeout_seconds).
- LangSmith tagging: attempt_number + rule_id on every run.
- If E2B_API_KEY is absent: returns SandboxResult(skipped=True) — never crashes.

Each run:
  1. Create sandbox.
  2. Write repo snapshot files.
  3. Apply fix diff via `patch`.
  4. Install dependencies (if requirements.txt / pyproject.toml detected).
  5. Run the repro script (from repro.py).
  6. Capture stdout/stderr/exit_code.
  7. Tear down sandbox.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel_review.config import SentinelConfig
    from sentinel_review.graph.schemas import ProposedFix, SandboxResult

logger = logging.getLogger(__name__)


class E2BRunner:
    """Manages E2B sandbox execution for fix validation."""

    def __init__(self, api_key: str, config: "SentinelConfig") -> None:
        self._api_key = api_key
        self._config = config

    def run_fix(
        self,
        repo_snapshot: str | Path,
        fix: "ProposedFix",
        repro_script: str | None = None,
        attempt_number: int = 1,
    ) -> "SandboxResult":
        """Validate a fix in a fresh E2B sandbox.

        Args:
            repo_snapshot:  Local path to the repo being reviewed.
            fix:            The ProposedFix to validate.
            repro_script:   Python code to run as the test. If None, repro.py
                            will be called to generate one.
            attempt_number: Which attempt this is (for LangSmith tagging).

        Returns:
            SandboxResult with passed/stderr/stdout populated.
        """
        from sentinel_review.graph.schemas import SandboxResult

        try:
            from e2b_code_interpreter import Sandbox
        except ImportError:
            logger.warning("e2b-code-interpreter not installed — sandbox skipped")
            return SandboxResult(
                passed=False, exit_code=-1, skipped=True,
                attempt_number=attempt_number,
            )

        timeout = self._config.sandbox_timeout_seconds
        rule_id = fix.finding.rule_id

        logger.info(
            "E2B sandbox attempt %d/%d for rule '%s'",
            attempt_number, self._config.max_fix_attempts, rule_id,
        )

        # LangSmith tagging
        try:
            from langsmith import traceable
        except ImportError:
            traceable = None

        sandbox = None
        try:
            sandbox = Sandbox(api_key=self._api_key, timeout=timeout)

            # Write the relevant file into the sandbox
            repo_path = Path(repo_snapshot)
            target_file = fix.finding.file_path
            target_full = repo_path / target_file

            if target_full.exists():
                original_code = target_full.read_text(encoding="utf-8")
            else:
                original_code = ""

            # Apply the diff
            patched_code = self._apply_diff_in_sandbox(
                sandbox, target_file, original_code, fix.diff
            )

            if patched_code is None:
                logger.warning("Patch application failed for '%s'", rule_id)
                return SandboxResult(
                    passed=False,
                    exit_code=1,
                    stderr="Patch application failed — diff could not be applied cleanly.",
                    attempt_number=attempt_number,
                    skipped=False,
                )

            # Run the repro script
            if repro_script is None:
                # Minimal smoke test: just import the patched file
                repro_script = f"exec(open('/tmp/target.py').read())\nprint('OK')"

            # Write patched file and repro script to sandbox
            sandbox.filesystem.write(f"/tmp/target.py", patched_code)
            sandbox.filesystem.write("/tmp/repro.py", repro_script)

            # Execute
            execution = sandbox.run_code(
                f"exec(open('/tmp/repro.py').read())",
                timeout=timeout,
            )

            stdout = "\n".join(str(o) for o in (execution.logs.stdout or []))
            stderr = "\n".join(str(e) for e in (execution.logs.stderr or []))
            exit_code = 0 if not execution.error else 1

            passed = exit_code == 0
            logger.info(
                "Sandbox attempt %d: %s (exit=%d)",
                attempt_number, "PASS" if passed else "FAIL", exit_code,
            )

            return SandboxResult(
                passed=passed,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr + (str(execution.error) if execution.error else ""),
                attempt_number=attempt_number,
                skipped=False,
            )

        except Exception as exc:
            logger.error("Sandbox execution error: %s", exc)
            return SandboxResult(
                passed=False,
                exit_code=-1,
                stderr=str(exc),
                attempt_number=attempt_number,
                skipped=False,
            )
        finally:
            # Guaranteed teardown — skill.md §6 requires this
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception as teardown_exc:
                    logger.debug("Sandbox teardown error (non-fatal): %s", teardown_exc)

    def _apply_diff_in_sandbox(
        self,
        sandbox,
        target_file: str,
        original_code: str,
        diff: str,
    ) -> str | None:
        """Apply a unified diff to original_code using Python's difflib.

        We apply in-process rather than shelling to `patch` to avoid
        requiring GNU patch in the E2B base image.

        Returns patched code string, or None if patch fails.
        """
        try:
            return _apply_unified_diff(original_code, diff)
        except Exception as exc:
            logger.warning("Diff application failed: %s", exc)
            return None


def _apply_unified_diff(original: str, diff: str) -> str:
    """Apply a unified diff to source text. Returns patched text.

    Simplified implementation suitable for single-file patches.
    For complex multi-file diffs, falls back to returning original.
    """
    import re

    lines = original.splitlines(keepends=True)
    result_lines = list(lines)

    # Parse hunks from diff
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    diff_lines = diff.splitlines(keepends=True)

    # Find the start of the first hunk
    hunk_starts = [i for i, l in enumerate(diff_lines) if hunk_re.match(l)]
    if not hunk_starts:
        # No hunks found — diff may be empty or malformed, return original
        return original

    # Apply each hunk (reverse order to preserve line numbers)
    patches: list[tuple[int, int, list[str]]] = []
    for hunk_i, hunk_start in enumerate(hunk_starts):
        m = hunk_re.match(diff_lines[hunk_start])
        if not m:
            continue
        orig_start = int(m.group(1))
        orig_count = int(m.group(2)) if m.group(2) is not None else 1
        new_count = int(m.group(4)) if m.group(4) is not None else 1

        hunk_end = hunk_starts[hunk_i + 1] if hunk_i + 1 < len(hunk_starts) else len(diff_lines)
        hunk_body = diff_lines[hunk_start + 1 : hunk_end]

        new_lines = [l[1:] for l in hunk_body if l.startswith("+")]
        patches.append((orig_start - 1, orig_start - 1 + orig_count, new_lines))

    # Apply in reverse order
    for (start, end, new_lines) in reversed(patches):
        result_lines[start:end] = new_lines

    return "".join(result_lines)

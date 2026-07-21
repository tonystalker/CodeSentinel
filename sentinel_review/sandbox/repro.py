"""
sentinel_review/sandbox/repro.py
==================================
Repro script generation for E2B sandbox validation (skill.md section 6).

Strategy:
1. If the target repo has test files covering the changed file, use those.
2. Otherwise, ask the LLM to generate a minimal repro script (ReproScript model).

This is a separate structured-output LLM call — not bundled with the fix
generation, so the fix agent and repro agent can be prompted independently.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sentinel_review.graph.schemas import Finding, ReproScript

logger = logging.getLogger(__name__)

_REPRO_SYSTEM_PROMPT = """\
You are generating a minimal Python test script to verify a bug fix.
Given a finding (bug or security issue) and the patched code, write a
self-contained script that:
1. Imports/executes the patched code
2. Exercises the specific code path that was buggy
3. Asserts the expected behavior (prints "OK" on success, raises AssertionError on failure)

The script must be runnable in a fresh Python environment with only stdlib available.
Keep it under 30 lines. Be specific to the bug — don't write a generic test.
"""

_REPRO_USER_TEMPLATE = """\
Finding:
  Rule: {rule_id}
  Severity: {severity}
  Description: {description}
  Location: {file_path}:{start_line}-{end_line}

Patched code:
{patched_code}

Write a minimal repro script that verifies this fix works correctly.
"""


def find_existing_tests(repo_path: Path, target_file: str) -> str | None:
    """Look for test files that cover the target file.

    Heuristic: test files named test_<module>.py or <module>_test.py
    in a tests/ directory.

    Returns:
        Path to the best matching test file, or None.
    """
    stem = Path(target_file).stem
    candidates = [
        f"test_{stem}.py",
        f"{stem}_test.py",
    ]

    for test_dir in ["tests", "test", "spec"]:
        test_root = repo_path / test_dir
        if not test_root.exists():
            continue
        for candidate in candidates:
            test_file = test_root / candidate
            if test_file.exists():
                logger.info("Found existing test: '%s'", test_file)
                return str(test_file)

    return None


def generate_repro_script(
    finding: Finding,
    patched_code: str,
    llm,
) -> ReproScript:
    """Generate a minimal repro script via the LLM.

    Args:
        finding:      The Finding being validated.
        patched_code: The patched source code (post-diff) to test.
        llm:          ChatGroq instance with .with_structured_output().

    Returns:
        ReproScript with code and expected_behavior fields.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    structured_llm = llm.with_structured_output(ReproScript)

    messages = [
        SystemMessage(content=_REPRO_SYSTEM_PROMPT),
        HumanMessage(
            content=_REPRO_USER_TEMPLATE.format(
                rule_id=finding.rule_id,
                severity=finding.severity,
                description=finding.description,
                file_path=finding.file_path,
                start_line=finding.start_line,
                end_line=finding.end_line,
                patched_code=patched_code[:3000],  # truncate for context window
            )
        ),
    ]

    try:
        result: ReproScript = structured_llm.invoke(messages)
        logger.info("Generated repro script (%d chars)", len(result.code))
        return result
    except Exception as exc:
        logger.warning("Repro script generation failed: %s — using fallback", exc)
        return ReproScript(
            code="print('OK')  # fallback: no repro script generated",
            expected_behavior="Script exits without error",
        )


def get_repro_script(
    repo_path: Path,
    finding: Finding,
    patched_code: str,
    llm,
) -> str:
    """Get the best available repro script for a finding.

    Tries existing test files first; falls back to LLM generation.

    Returns:
        Python code string to run in the sandbox.
    """
    # Check for existing tests
    existing = find_existing_tests(repo_path, finding.file_path)
    if existing:
        test_path = Path(existing)
        if test_path.exists():
            code = test_path.read_text(encoding="utf-8")
            logger.info("Using existing test file: '%s'", existing)
            return f"import subprocess, sys\nresult = subprocess.run([sys.executable, '-m', 'pytest', '{existing}', '-x', '-q'], capture_output=True)\nprint(result.stdout.decode())\nif result.returncode != 0: raise SystemExit(result.returncode)"

    # Fall back to LLM-generated repro
    repro = generate_repro_script(finding, patched_code, llm)
    return repro.code

"""
sentinel_review/graph/gating.py
================================
Confidence-gated auto-fix decision. Lives in exactly one place (skill.md §5).

If should_auto_apply() returns False, the fix is still included in the report
as a suggestion with the diff shown — never silently dropped.
"""
from __future__ import annotations

from sentinel_review.config import SentinelConfig
from sentinel_review.graph.schemas import ProposedFix, SandboxResult


def should_auto_apply(
    fix: ProposedFix,
    sandbox_result: SandboxResult,
    config: SentinelConfig,
) -> bool:
    """Decide whether to automatically apply a generated fix.

    All three conditions must hold:
      1. Sandbox run passed (exit code 0, not skipped).
      2. Model confidence ≥ configured threshold (default 0.85).
      3. Finding severity is in the allowed list (default: not 'critical').

    Args:
        fix:            The proposed fix, including finding.severity and confidence.
        sandbox_result: Result of the E2B sandbox run for this fix.
        config:         Loaded SentinelConfig with gate thresholds.

    Returns:
        True if the fix should be automatically applied; False otherwise.
    """
    if sandbox_result.skipped:
        # No sandbox available — never auto-apply unvalidated fixes
        return False

    if not sandbox_result.passed:
        return False

    if fix.confidence < config.auto_fix_confidence_threshold:
        return False

    if fix.finding.severity not in config.auto_fix_allowed_severities:
        return False

    return True


def gate_description(
    fix: ProposedFix,
    sandbox_result: SandboxResult,
    config: SentinelConfig,
) -> str:
    """Return a human-readable explanation of the gate decision."""
    reasons = []

    if sandbox_result.skipped:
        reasons.append("sandbox not available (no E2B_API_KEY)")
    elif not sandbox_result.passed:
        reasons.append(f"sandbox failed (exit {sandbox_result.exit_code})")

    if fix.confidence < config.auto_fix_confidence_threshold:
        reasons.append(
            f"confidence {fix.confidence:.2f} < threshold {config.auto_fix_confidence_threshold:.2f}"
        )

    if fix.finding.severity not in config.auto_fix_allowed_severities:
        reasons.append(
            f"severity '{fix.finding.severity}' not in auto_fix_allowed_severities "
            f"{config.auto_fix_allowed_severities}"
        )

    if not reasons:
        return "auto-apply: all conditions met"
    return "suggestion only: " + "; ".join(reasons)

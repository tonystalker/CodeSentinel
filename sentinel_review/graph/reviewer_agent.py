"""
sentinel_review/graph/reviewer_agent.py
==========================================
Reviewer agent — aggregation (fan-in) and deterministic scoring.

Runs after all other agents have completed. Two responsibilities:
1. Deduplicate findings (same file+line+rule_id from multiple agents).
2. Compute deterministic severity/confidence scores (NOT LLM freehand).

Scoring formula:
  review_score = weighted_severity_sum / max_possible
  where weights: critical=4, high=3, medium=2, low=1
  capped at 1.0 (so 5 high findings doesn't make it "worse than 1 critical")

fix_success_rate = validated_fixes / total_fixes  (0.0 if no fixes)
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sentinel_review.graph.schemas import Finding, ProposedFix, SandboxResult
from sentinel_review.graph.state import AgentState

logger = logging.getLogger(__name__)

_SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    """Remove duplicate findings with the same (file_path, start_line, rule_id)."""
    seen: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.file_path, f.start_line, f.rule_id)
        if key not in seen:
            seen[key] = f
        else:
            # Keep the one with higher severity
            existing = seen[key]
            if _SEVERITY_WEIGHT.get(f.severity, 0) > _SEVERITY_WEIGHT.get(existing.severity, 0):
                seen[key] = f
    return list(seen.values())


def _compute_review_score(findings: list[Finding]) -> float:
    """Deterministic severity score normalised to 0.0–1.0.

    A single critical finding → 1.0. Many low findings → low score.
    """
    if not findings:
        return 0.0
    total_weight = sum(_SEVERITY_WEIGHT.get(f.severity, 1) for f in findings)
    # Normalise: treat 1 critical finding (weight=4) as 1.0
    return min(total_weight / 4.0, 1.0)


def _compute_fix_success_rate(
    fixes: list[ProposedFix],
    sandbox_results: list[SandboxResult],
) -> float:
    """Fraction of fixes whose sandbox run passed. 0.0 if no fixes."""
    if not fixes:
        return 0.0
    # Map rule_id → best sandbox result
    rule_passed: dict[str, bool] = {}
    for sr in sandbox_results:
        if sr.skipped:
            continue
        # We don't have a direct fix→result link in state yet; approximate by attempt
        # TODO(Task 6): wire finding.rule_id → sandbox_result directly
    # For now: fraction of sandbox_results that passed (not skipped)
    non_skipped = [sr for sr in sandbox_results if not sr.skipped]
    if not non_skipped:
        return 0.0
    return sum(1 for sr in non_skipped if sr.passed) / len(non_skipped)


def run(state: AgentState, llm=None) -> dict:
    """Reviewer agent node (fan-in). Runs after all agents complete.

    llm is accepted for API uniformity but not used — scoring is deterministic.
    """
    raw_findings: list[Finding] = state.get("findings", [])
    fixes: list[ProposedFix] = state.get("fixes", [])
    sandbox_results: list[SandboxResult] = state.get("sandbox_results", [])

    deduped = _deduplicate(raw_findings)
    review_score = _compute_review_score(deduped)
    fix_success_rate = _compute_fix_success_rate(fixes, sandbox_results)

    n_removed = len(raw_findings) - len(deduped)
    if n_removed:
        logger.info("Reviewer: removed %d duplicate findings", n_removed)

    logger.info(
        "Reviewer: %d findings, score=%.3f, fix_success_rate=%.3f",
        len(deduped), review_score, fix_success_rate,
    )

    return {
        "deduplicated_findings": deduped,  # Plain field: last-writer-wins (no accumulation)
        "review_score": review_score,
        "fix_success_rate": fix_success_rate,
        "agent_timeline": [
            f"reviewer_agent: {len(deduped)} findings, score={review_score:.3f}"
        ],
    }

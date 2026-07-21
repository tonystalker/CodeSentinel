"""
sentinel_review/report/json_report.py
=======================================
Canonical JSON report — the source of truth for all outputs.

SARIF, CLI display, and future PDF/HTML views are generated FROM this,
not from agent state directly. This ensures a single serialisation point
and makes the report format independently testable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sentinel_review.graph.schemas import Finding, ProposedFix, SandboxResult
from sentinel_review.graph.gating import gate_description, should_auto_apply
from sentinel_review.config import SentinelConfig


def build_report(
    namespace: str,
    findings: list[Finding],
    fixes: list[ProposedFix],
    sandbox_results: list[SandboxResult],
    review_score: float | None,
    fix_success_rate: float | None,
    config: SentinelConfig,
    agent_timeline: list[str] | None = None,
) -> dict:
    """Build the canonical JSON report dict.

    Args:
        namespace:        Repo identifier ("owner/repo").
        findings:         Deduplicated findings from reviewer_agent.
        fixes:            Generated ProposedFix objects.
        sandbox_results:  Sandbox validation results.
        review_score:     Severity-weighted score 0.0–1.0 from reviewer_agent.
        fix_success_rate: Fraction of fixes that passed sandbox.
        config:           SentinelConfig (for gate evaluation).
        agent_timeline:   List of agent step descriptions for CLI display.

    Returns:
        JSON-serialisable dict representing the full report.
    """
    # Build sandbox result lookup by attempt (approximate — Task 6 wires exact link)
    sr_by_idx: dict[int, SandboxResult] = {
        sr.attempt_number: sr for sr in sandbox_results
    }

    # Annotate fixes with gate decision
    annotated_fixes = []
    for i, fix in enumerate(fixes):
        sr = sandbox_results[i] if i < len(sandbox_results) else None
        if sr is None:
            sr = SandboxResult(passed=False, exit_code=-1, skipped=True, attempt_number=1)

        auto_apply = should_auto_apply(fix, sr, config) if config.auto_fix else False
        gate_desc = gate_description(fix, sr, config)

        annotated_fixes.append({
            "finding": fix.finding.model_dump(),
            "diff": fix.diff,
            "confidence": fix.confidence,
            "reasoning": fix.reasoning,
            "sandbox": {
                "passed": sr.passed,
                "exit_code": sr.exit_code,
                "skipped": sr.skipped,
                "attempt_number": sr.attempt_number,
                "stderr": sr.stderr[:2000] if sr.stderr else "",
            },
            "auto_apply": auto_apply,
            "gate_decision": gate_desc,
        })

    return {
        "sentinel_review_version": "0.1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "summary": {
            "total_findings": len(findings),
            "by_severity": _count_by_severity(findings),
            "by_category": _count_by_category(findings),
            "review_score": review_score,
            "fix_success_rate": fix_success_rate,
            "fixes_generated": len(fixes),
            "fixes_auto_applied": sum(1 for fx in annotated_fixes if fx["auto_apply"]),
        },
        "findings": [f.model_dump() for f in findings],
        "fixes": annotated_fixes,
        "agent_timeline": agent_timeline or [],
    }


def write_report(report: dict, output_path: Path) -> None:
    """Write the canonical JSON report to a file."""
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)


def _count_by_severity(findings: list[Finding]) -> dict:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _count_by_category(findings: list[Finding]) -> dict:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.category] = counts.get(f.category, 0) + 1
    return counts

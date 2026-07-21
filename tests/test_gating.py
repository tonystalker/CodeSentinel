"""
tests/test_gating.py
======================
Tests for the confidence-gated auto-fix decision (skill.md §5).
gate lives in gating.py and nowhere else.
"""
from __future__ import annotations

import pytest

from sentinel_review.config import SentinelConfig
from sentinel_review.graph.gating import gate_description, should_auto_apply
from sentinel_review.graph.schemas import Finding, ProposedFix, SandboxResult


def make_finding(severity: str = "high", rule_id: str = "null-deref") -> Finding:
    return Finding(
        file_path="test.py",
        start_line=1,
        end_line=5,
        severity=severity,
        category="bug",
        description="Test finding",
        rule_id=rule_id,
    )


def make_fix(confidence: float = 0.90, severity: str = "high") -> ProposedFix:
    return ProposedFix(
        finding=make_finding(severity=severity),
        diff="--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-bad\n+good",
        confidence=confidence,
        reasoning="Test fix",
    )


def make_sandbox(passed: bool = True, skipped: bool = False) -> SandboxResult:
    return SandboxResult(
        passed=passed,
        exit_code=0 if passed else 1,
        skipped=skipped,
        attempt_number=1,
    )


class TestShouldAutoApply:
    def test_all_conditions_met_returns_true(self) -> None:
        config = SentinelConfig(auto_fix=True, auto_fix_confidence_threshold=0.85)
        assert should_auto_apply(make_fix(0.90, "high"), make_sandbox(passed=True), config)

    def test_sandbox_failed_returns_false(self) -> None:
        config = SentinelConfig(auto_fix=True)
        assert not should_auto_apply(make_fix(0.95), make_sandbox(passed=False), config)

    def test_sandbox_skipped_returns_false(self) -> None:
        config = SentinelConfig(auto_fix=True)
        assert not should_auto_apply(make_fix(0.95), make_sandbox(skipped=True), config)

    def test_low_confidence_returns_false(self) -> None:
        config = SentinelConfig(auto_fix=True, auto_fix_confidence_threshold=0.85)
        assert not should_auto_apply(make_fix(0.70), make_sandbox(passed=True), config)

    def test_critical_severity_blocked_by_default(self) -> None:
        """Default config excludes 'critical' from auto_fix_allowed_severities."""
        config = SentinelConfig(auto_fix=True)
        assert "critical" not in config.auto_fix_allowed_severities
        critical_fix = make_fix(confidence=0.99, severity="critical")
        assert not should_auto_apply(critical_fix, make_sandbox(passed=True), config)

    def test_critical_severity_allowed_when_explicitly_configured(self) -> None:
        config = SentinelConfig(
            auto_fix=True,
            auto_fix_allowed_severities=["critical", "high", "medium", "low"],
            auto_fix_confidence_threshold=0.85,
        )
        critical_fix = make_fix(confidence=0.95, severity="critical")
        assert should_auto_apply(critical_fix, make_sandbox(passed=True), config)

    def test_confidence_exactly_at_threshold_passes(self) -> None:
        config = SentinelConfig(auto_fix=True, auto_fix_confidence_threshold=0.85)
        fix = make_fix(confidence=0.85)
        assert should_auto_apply(fix, make_sandbox(passed=True), config)

    def test_confidence_just_below_threshold_fails(self) -> None:
        config = SentinelConfig(auto_fix=True, auto_fix_confidence_threshold=0.85)
        fix = make_fix(confidence=0.8499)
        assert not should_auto_apply(fix, make_sandbox(passed=True), config)


class TestGateDescription:
    def test_all_pass_returns_auto_apply_message(self) -> None:
        config = SentinelConfig(auto_fix=True, auto_fix_confidence_threshold=0.85)
        desc = gate_description(make_fix(0.90), make_sandbox(passed=True), config)
        assert "auto-apply" in desc.lower()

    def test_skipped_sandbox_in_description(self) -> None:
        config = SentinelConfig(auto_fix=True)
        desc = gate_description(make_fix(0.90), make_sandbox(skipped=True), config)
        assert "e2b" in desc.lower() or "sandbox" in desc.lower()

    def test_low_confidence_in_description(self) -> None:
        config = SentinelConfig(auto_fix=True, auto_fix_confidence_threshold=0.85)
        desc = gate_description(make_fix(0.70), make_sandbox(passed=True), config)
        assert "confidence" in desc.lower()

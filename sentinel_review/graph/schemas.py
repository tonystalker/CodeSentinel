"""
sentinel_review/graph/schemas.py
=================================
Pydantic models for structured LLM output. Every agent returns one of these.
Never parse free text — use .with_structured_output() always (skill.md §2).

Finding.rule_id must be a stable slug (e.g. "null-deref", "sql-injection").
SARIF output uses it directly as ruleId and as the rule registry key (skill.md §7).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Finding(BaseModel):
    """A single code issue identified by a review agent."""

    file_path: str = Field(description="Relative path to the file containing the issue.")
    start_line: int = Field(description="1-indexed line where the issue begins.")
    end_line: int = Field(description="1-indexed line where the issue ends (inclusive).")
    severity: Literal["critical", "high", "medium", "low"] = Field(
        description="Issue severity: critical > high > medium > low."
    )
    category: Literal["bug", "security", "docs"] = Field(
        description="Which agent type identified this finding."
    )
    description: str = Field(
        description="Human-readable description of the issue. Be specific and actionable."
    )
    rule_id: str = Field(
        description=(
            "Stable slug identifier for this rule (e.g. 'null-deref', 'sql-injection', "
            "'missing-import'). Used as SARIF ruleId. Must be lowercase, hyphen-separated."
        )
    )

    @field_validator("rule_id")
    @classmethod
    def rule_id_is_slug(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z][a-z0-9-]*$", v):
            # Normalise rather than reject — LLMs sometimes produce CamelCase or underscores
            v = re.sub(r"[^a-z0-9-]", "-", v.lower()).strip("-")
        return v

    @field_validator("start_line", "end_line")
    @classmethod
    def line_positive(cls, v: int) -> int:
        if v < 1:
            return 1
        return v

    model_config = {"extra": "ignore"}


class ProposedFix(BaseModel):
    """A generated fix for a Finding, with sandbox-validation metadata."""

    finding: Finding = Field(description="The finding this fix addresses.")
    diff: str = Field(
        description=(
            "Unified diff format patch. Must be applicable with `patch -p1`. "
            "Include the --- a/ and +++ b/ header lines."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Model's own calibrated confidence in this fix (0.0–1.0). "
            "Used by the auto-apply gate: below config.auto_fix_confidence_threshold → suggestion only."
        ),
    )
    reasoning: str = Field(
        description="Brief explanation of why this fix addresses the root cause."
    )

    model_config = {"extra": "ignore"}


class SandboxResult(BaseModel):
    """Result of running a fix through the E2B sandbox."""

    passed: bool = Field(description="True if the sandbox run exited with code 0.")
    exit_code: int = Field(description="Process exit code.")
    stdout: str = Field(default="", description="Captured standard output.")
    stderr: str = Field(default="", description="Captured standard error / traceback.")
    attempt_number: int = Field(default=1, description="Which attempt this was (1-indexed).")
    skipped: bool = Field(
        default=False,
        description="True if E2B_API_KEY was absent — fix is unvalidated, not failed.",
    )

    @property
    def failure_context(self) -> str:
        """Formatted stderr suitable for feeding back into fix_agent as retry context."""
        if self.skipped:
            return "(sandbox not available — fix not validated)"
        return f"Exit code {self.exit_code}\n\nSTDERR:\n{self.stderr}"


class ReproScript(BaseModel):
    """LLM-generated minimal reproduction script for a Finding."""

    code: str = Field(
        description="A self-contained Python script that reproduces or tests the bug."
    )
    expected_behavior: str = Field(
        description="What the script should output/do when the bug is fixed."
    )

    model_config = {"extra": "ignore"}

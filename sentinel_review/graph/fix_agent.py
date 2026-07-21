"""
sentinel_review/graph/fix_agent.py
=====================================
Fix generation agent node.

Takes a list of findings and generates unified-diff patches for each.
Uses structured output to ensure the diff field is machine-parseable.

Design: generates fixes for all findings in one LLM call (batched) for
efficiency, but structures output so each finding maps to exactly one
ProposedFix. If a finding has no clear fix, it is omitted from output
(the reviewer_agent will mark it as "no fix available").
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from sentinel_review.graph.schemas import Finding, ProposedFix
from sentinel_review.graph.state import AgentState
from sentinel_review.ingestion.parser import CodeChunk

logger = logging.getLogger(__name__)

_FIX_SYSTEM_PROMPT = """\
You are an expert software engineer tasked with generating code fixes.
For each finding provided, generate a unified diff patch that fixes the issue.

Rules:
- Diff must be in standard unified diff format (--- a/file, +++ b/file, @@ ... @@)
- The patch must be minimal — change only what is needed to fix the bug
- confidence: your calibrated 0.0–1.0 estimate that this diff is correct and complete
  (be honest — if you're uncertain, say 0.5–0.6, not 0.9)
- reasoning: one or two sentences explaining the root cause and your fix approach
- If you cannot generate a safe fix for a finding, skip it

Previous attempt failure context (if any): {failure_context}
"""

_FIX_USER_TEMPLATE = """\
Generate fixes for the following findings. Repo: {namespace}

FINDINGS:
{findings_text}

RELEVANT CODE CONTEXT:
{chunks_text}

Return structured output with one ProposedFix per finding you can fix.
"""


class FixBatch(BaseModel):
    fixes: list[ProposedFix]


def _format_findings(findings: list[Finding]) -> str:
    parts = []
    for i, f in enumerate(findings, 1):
        parts.append(
            f"{i}. [{f.severity.upper()}] {f.rule_id} @ {f.file_path}:{f.start_line}-{f.end_line}\n"
            f"   {f.description}"
        )
    return "\n".join(parts)


def _format_chunks(chunks: list[CodeChunk]) -> str:
    parts = []
    for chunk in chunks:
        parts.append(
            f"=== {chunk.file_path}:{chunk.start_line}-{chunk.end_line} ===\n{chunk.code}"
        )
    return "\n".join(parts)


def run(state: AgentState, llm, failure_context: str = "") -> dict:
    """Fix agent node.

    Args:
        state:           Current AgentState.
        llm:             ChatGroq instance.
        failure_context: On retry, the previous attempt's stderr for context.

    Returns:
        Partial state update with fixes list extended.
    """
    findings = state.get("findings", [])
    chunks = state.get("chunks", [])
    namespace = state.get("namespace", "unknown")

    if not findings:
        return {"fixes": [], "agent_timeline": ["fix_agent: skipped (no findings)"]}

    structured_llm = llm.with_structured_output(FixBatch)

    from langchain_core.messages import HumanMessage, SystemMessage
    messages = [
        SystemMessage(
            content=_FIX_SYSTEM_PROMPT.format(
                failure_context=failure_context or "(first attempt — no prior errors)"
            )
        ),
        HumanMessage(
            content=_FIX_USER_TEMPLATE.format(
                namespace=namespace,
                findings_text=_format_findings(findings),
                chunks_text=_format_chunks(chunks),
            )
        ),
    ]

    try:
        result: FixBatch = structured_llm.invoke(messages)
        fixes = result.fixes
        logger.info("Fix agent generated %d fix(es) for %d finding(s)", len(fixes), len(findings))
    except Exception as exc:
        logger.error("Fix agent LLM call failed: %s", exc)
        fixes = []

    return {
        "fixes": fixes,
        "agent_timeline": [f"fix_agent: {len(fixes)} fix(es) generated"],
    }

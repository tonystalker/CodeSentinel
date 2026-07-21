"""
sentinel_review/graph/bug_agent.py
====================================
Bug detection agent node for the LangGraph workflow.

Uses Groq Llama 3.3 70B with structured output (.with_structured_output())
per skill.md section 2. Never hand-rolls JSON prompting.

Returns AgentState with findings list extended (operator.add reducer in state.py).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from sentinel_review.graph.schemas import Finding
from sentinel_review.graph.state import AgentState
from sentinel_review.ingestion.parser import CodeChunk

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BUG_SYSTEM_PROMPT = """\
You are an expert code reviewer specialising in bug detection.
Analyse the provided code chunks and identify concrete bugs — not style issues or
potential improvements, but actual defects that would cause incorrect behavior,
crashes, or data loss at runtime.

For each bug found, provide:
- The exact file path and line range
- Severity (critical/high/medium/low)
- A specific, actionable description (include the variable/expression at fault)
- A stable rule_id slug (e.g. "null-deref", "missing-import", "off-by-one",
  "unhandled-exception", "type-mismatch", "resource-leak")

Only report findings you are confident about. Do not hallucinate bugs.
"""

_BUG_USER_TEMPLATE = """\
Review the following code chunks for bugs. Repo: {namespace}

{chunks_text}

Return your findings as structured output. If no bugs found, return an empty findings list.
"""


class BugFindings(BaseModel):
    """Wrapper so .with_structured_output() gets a top-level object."""
    findings: list[Finding]


def _format_chunks(chunks: list[CodeChunk]) -> str:
    parts = []
    for chunk in chunks:
        parts.append(
            f"=== {chunk.file_path}:{chunk.start_line}-{chunk.end_line} ({chunk.name}) ===\n"
            f"{chunk.code}\n"
        )
    return "\n".join(parts)


def run(state: AgentState, llm) -> dict:
    """Bug agent node. Runs independently (fan-out).

    Args:
        state: Current AgentState.
        llm:   ChatGroq instance (passed by build_graph.py).

    Returns:
        Partial state update dict — findings list extended.
    """
    chunks = state.get("chunks", [])
    namespace = state.get("namespace", "unknown")

    if not chunks:
        logger.info("Bug agent: no chunks to review")
        return {"findings": [], "agent_timeline": ["bug_agent: skipped (no chunks)"]}

    structured_llm = llm.with_structured_output(BugFindings)

    from langchain_core.messages import HumanMessage, SystemMessage
    messages = [
        SystemMessage(content=_BUG_SYSTEM_PROMPT),
        HumanMessage(
            content=_BUG_USER_TEMPLATE.format(
                namespace=namespace,
                chunks_text=_format_chunks(chunks),
            )
        ),
    ]

    try:
        result: BugFindings = structured_llm.invoke(messages)
        findings = result.findings
        logger.info("Bug agent found %d finding(s)", len(findings))
    except Exception as exc:
        logger.error("Bug agent LLM call failed: %s", exc)
        findings = []

    return {
        "findings": findings,
        "agent_timeline": [f"bug_agent: {len(findings)} finding(s)"],
    }

"""
sentinel_review/graph/docs_agent.py
=====================================
Documentation quality agent node.

Identifies missing, incorrect, or misleading docstrings. Low severity
by nature — docs findings are "medium" at most. Only flags issues that
would genuinely confuse a maintainer or user of an API.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from sentinel_review.graph.schemas import Finding
from sentinel_review.graph.state import AgentState
from sentinel_review.ingestion.parser import CodeChunk

logger = logging.getLogger(__name__)

_DOCS_SYSTEM_PROMPT = """\
You are a technical documentation reviewer for software code.
Analyse the provided code chunks and identify documentation issues.

Focus on:
- Public functions/classes with no docstring at all
- Docstrings that describe wrong return types, parameters, or behavior
- Docstrings that are empty or contain only placeholder text
- Missing exception documentation for functions that clearly raise exceptions
- API functions with complex parameters that have no parameter docs

Do NOT flag:
- Private functions (_name) that lack docstrings — this is acceptable
- Short, obvious utility functions (< 5 lines)
- Test functions

Rule IDs: "missing-docstring", "incorrect-docstring", "empty-docstring",
"missing-param-docs", "missing-exception-docs".

Severity: at most "medium" for public API functions, "low" for internal ones.
"""

_DOCS_USER_TEMPLATE = """\
Review the following code chunks for documentation issues. Repo: {namespace}

{chunks_text}

Return your findings as structured output. If no issues found, return an empty list.
"""


class DocsFindings(BaseModel):
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
    """Docs agent node. Runs independently (fan-out)."""
    chunks = state.get("chunks", [])
    namespace = state.get("namespace", "unknown")

    if not chunks:
        return {"findings": [], "agent_timeline": ["docs_agent: skipped (no chunks)"]}

    structured_llm = llm.with_structured_output(DocsFindings)

    from langchain_core.messages import HumanMessage, SystemMessage
    messages = [
        SystemMessage(content=_DOCS_SYSTEM_PROMPT),
        HumanMessage(
            content=_DOCS_USER_TEMPLATE.format(
                namespace=namespace,
                chunks_text=_format_chunks(chunks),
            )
        ),
    ]

    try:
        result: DocsFindings = structured_llm.invoke(messages)
        findings = result.findings
        logger.info("Docs agent found %d finding(s)", len(findings))
    except Exception as exc:
        logger.error("Docs agent LLM call failed: %s", exc)
        findings = []

    return {
        "findings": findings,
        "agent_timeline": [f"docs_agent: {len(findings)} finding(s)"],
    }

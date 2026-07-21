"""
sentinel_review/graph/security_agent.py
=========================================
Security vulnerability detection agent node.

Focuses on OWASP Top 10 and common security antipatterns:
injection, broken auth, insecure deserialization, hardcoded secrets, etc.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from sentinel_review.graph.schemas import Finding
from sentinel_review.graph.state import AgentState
from sentinel_review.ingestion.parser import CodeChunk

logger = logging.getLogger(__name__)

_SECURITY_SYSTEM_PROMPT = """\
You are an expert code security reviewer specialising in vulnerability detection.
Analyse the provided code chunks and identify security vulnerabilities.

Focus on:
- Injection vulnerabilities (SQL, command, LDAP, XPath injection)
- Hardcoded secrets, API keys, passwords
- Insecure cryptography (weak algorithms, static IVs, predictable keys)
- Broken authentication or session management
- Path traversal and file inclusion
- Insecure deserialization
- Missing input validation / sanitization
- Information disclosure in logs or error messages
- Race conditions on security-critical state
- SSRF (server-side request forgery)

Rule ID examples: "sql-injection", "hardcoded-secret", "weak-crypto",
"path-traversal", "insecure-deserial", "missing-auth", "command-injection".

Report only confirmed vulnerabilities, not theoretical ones.
"""

_SECURITY_USER_TEMPLATE = """\
Review the following code chunks for security vulnerabilities. Repo: {namespace}

{chunks_text}

Return your findings as structured output. If no vulnerabilities found, return an empty list.
"""


class SecurityFindings(BaseModel):
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
    """Security agent node. Runs independently (fan-out)."""
    chunks = state.get("chunks", [])
    namespace = state.get("namespace", "unknown")

    if not chunks:
        return {"findings": [], "agent_timeline": ["security_agent: skipped (no chunks)"]}

    structured_llm = llm.with_structured_output(SecurityFindings)

    from langchain_core.messages import HumanMessage, SystemMessage
    messages = [
        SystemMessage(content=_SECURITY_SYSTEM_PROMPT),
        HumanMessage(
            content=_SECURITY_USER_TEMPLATE.format(
                namespace=namespace,
                chunks_text=_format_chunks(chunks),
            )
        ),
    ]

    try:
        result: SecurityFindings = structured_llm.invoke(messages)
        findings = result.findings
        logger.info("Security agent found %d finding(s)", len(findings))
    except Exception as exc:
        logger.error("Security agent LLM call failed: %s", exc)
        findings = []

    return {
        "findings": findings,
        "agent_timeline": [f"security_agent: {len(findings)} finding(s)"],
    }

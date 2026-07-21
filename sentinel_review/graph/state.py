"""
sentinel_review/graph/state.py
================================
Shared AgentState TypedDict passed through the LangGraph workflow.

Design: all agents read from and write to this dict. LangGraph merges
partial updates returned from each node. Lists are accumulated (not replaced)
by using operator.add as the reducer.

Key design decision on findings:
- `findings` (Annotated with operator.add) accumulates raw outputs from all
  fan-out agents (bug, security, docs). This may contain duplicates.
- `deduplicated_findings` (plain list, last-writer-wins) is written by
  reviewer_agent after deduplication. Downstream (CLI, reports) should read
  from `deduplicated_findings`, not `findings`, for the final result set.
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from sentinel_review.graph.schemas import Finding, ProposedFix, SandboxResult
from sentinel_review.ingestion.parser import CodeChunk


class AgentState(TypedDict, total=False):
    """Shared state threaded through the LangGraph workflow.

    Fields with Annotated[list, operator.add] are accumulated across nodes.
    All other fields are last-writer-wins.
    """

    # --- Input context ---
    repo_path: str                   # Absolute path to the local repo being reviewed
    namespace: str                   # Vector store namespace, e.g. "owner/repo"
    chunks: list[CodeChunk]          # Chunks selected for this run (post diff-selection)

    # --- Retrieved context (filled by retrieval step before agents) ---
    retrieved_chunks: list[CodeChunk]  # Top-k similar chunks from vector store

    # --- Agent outputs (accumulated across fan-out nodes) ---
    findings: Annotated[list[Finding], operator.add]      # Raw, may have duplicates
    fixes: Annotated[list[ProposedFix], operator.add]
    sandbox_results: Annotated[list[SandboxResult], operator.add]

    # --- Reviewer output (last-writer-wins, deduped) ---
    deduplicated_findings: list[Finding]  # Set by reviewer_agent; use this for reports

    # --- Retry loop state ---
    retry_count: int                  # Current fix-attempt count (reset per finding)
    current_fix: ProposedFix | None   # Fix being validated in the retry loop

    # --- Final scores (set by reviewer_agent) ---
    review_score: float | None        # Weighted severity score 0.0–1.0
    fix_success_rate: float | None    # Fraction of fixes that passed sandbox
    agent_timeline: list[str]         # Human-readable log of agent transitions (for CLI display)

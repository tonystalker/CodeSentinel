"""
sentinel_review/graph/build_graph.py
======================================
Wires all agent nodes into the LangGraph StateGraph.

Topology:
  START
    → [bug_agent, security_agent, docs_agent]  (fan-out, parallel)
    → fix_agent                                 (fan-in after all 3 complete)
    → sandbox_validate                          (E2B or no-op)
    → [retry? fix_agent | continue]             (conditional retry loop)
    → reviewer_agent                            (scoring + dedup)
  END

Fan-out is implemented with LangGraph's Send API (parallel execution).
The retry loop feeds sandbox stderr back into fix_agent as failure_context.

Task 6 plugs in the actual E2B execution at the sandbox_validate node;
until Task 6 is complete, it's a no-op that marks all results as skipped.
"""
from __future__ import annotations

import logging
from functools import partial

from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph

from sentinel_review.config import SentinelConfig
from sentinel_review.graph import (
    bug_agent,
    docs_agent,
    fix_agent,
    reviewer_agent,
    security_agent,
)
from sentinel_review.graph.schemas import SandboxResult
from sentinel_review.graph.state import AgentState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node wrappers (bind llm and config via partial / closure)
# ---------------------------------------------------------------------------

def _make_bug_node(llm):
    def node(state: AgentState) -> dict:
        return bug_agent.run(state, llm)
    return node


def _make_security_node(llm):
    def node(state: AgentState) -> dict:
        return security_agent.run(state, llm)
    return node


def _make_docs_node(llm):
    def node(state: AgentState) -> dict:
        return docs_agent.run(state, llm)
    return node


def _make_fix_node(llm):
    def node(state: AgentState) -> dict:
        failure_context = ""
        # If this is a retry, pull the last sandbox failure context
        sandbox_results = state.get("sandbox_results", [])
        if sandbox_results:
            last = sandbox_results[-1]
            if not last.passed and not last.skipped:
                failure_context = last.failure_context
        return fix_agent.run(state, llm, failure_context=failure_context)
    return node


def _make_sandbox_node(e2b_runner=None):
    """Create the sandbox validation node.

    If e2b_runner is None (Task 6 not yet integrated), marks all fixes
    as skipped (no sandbox available).
    """
    def node(state: AgentState) -> dict:
        fixes = state.get("fixes", [])
        if not fixes:
            return {"sandbox_results": [], "agent_timeline": ["sandbox: no fixes to validate"]}

        if e2b_runner is None:
            # Task 6 stub — no sandbox, mark as skipped
            results = [
                SandboxResult(
                    passed=False,
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    attempt_number=state.get("retry_count", 1),
                    skipped=True,
                )
                for _ in fixes
            ]
            return {
                "sandbox_results": results,
                "agent_timeline": ["sandbox: skipped (no E2B runner configured)"],
            }

        # Task 6: call e2b_runner for each fix
        results = []
        attempt = state.get("retry_count", 1)
        for fix in fixes:
            result = e2b_runner.run_fix(
                repo_snapshot=state.get("repo_path", ""),
                fix=fix,
                attempt_number=attempt,
            )
            results.append(result)
        return {
            "sandbox_results": results,
            "retry_count": attempt,
            "agent_timeline": [f"sandbox: {sum(1 for r in results if r.passed)}/{len(results)} passed"],
        }
    return node


def _make_reviewer_node(llm):
    def node(state: AgentState) -> dict:
        return reviewer_agent.run(state, llm)
    return node


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _should_retry(state: AgentState, max_attempts: int) -> str:
    """Conditional edge: retry fix_agent or proceed to reviewer."""
    sandbox_results = state.get("sandbox_results", [])
    retry_count = state.get("retry_count", 1)

    # If any sandbox result failed (not skipped), and we have retries left
    any_failed = any(
        not sr.passed and not sr.skipped for sr in sandbox_results
    )
    if any_failed and retry_count < max_attempts:
        return "retry"
    return "done"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph(
    config: SentinelConfig,
    groq_api_key: str,
    e2b_runner=None,
    enabled_agents: list[str] | None = None,
):
    """Construct and compile the LangGraph StateGraph.

    Args:
        config:        Loaded SentinelConfig.
        groq_api_key:  GROQ_API_KEY value (from SentinelSecrets).
        e2b_runner:    Optional E2B runner (Task 6). None → sandbox is a no-op.
        enabled_agents: Override config.enabled_agents for this run.

    Returns:
        A compiled LangGraph CompiledGraph ready for .invoke() / .stream().
    """
    import os
    model = os.environ.get("SENTINEL_LLM_MODEL") or config.llm_model
    llm = ChatGroq(
        model=model,
        api_key=groq_api_key,
        temperature=0,
    )
    logger.info("LLM: %s", model)


    agents = enabled_agents or config.enabled_agents
    max_attempts = config.max_fix_attempts

    graph = StateGraph(AgentState)

    # --- Add agent nodes ---
    if "bug" in agents:
        graph.add_node("bug_agent", _make_bug_node(llm))
    if "security" in agents:
        graph.add_node("security_agent", _make_security_node(llm))
    if "docs" in agents:
        graph.add_node("docs_agent", _make_docs_node(llm))

    graph.add_node("fix_agent", _make_fix_node(llm))
    graph.add_node("sandbox_validate", _make_sandbox_node(e2b_runner))
    graph.add_node("reviewer_agent", _make_reviewer_node(llm))

    # --- Edges: START → fan-out ---
    if "bug" in agents:
        graph.add_edge(START, "bug_agent")
    if "security" in agents:
        graph.add_edge(START, "security_agent")
    if "docs" in agents:
        graph.add_edge(START, "docs_agent")

    # --- Edges: fan-out → fix_agent ---
    # All active analysis agents must complete before fix_agent starts
    active_agents = [f"{a}_agent" for a in ["bug", "security", "docs"] if a in agents]
    for agent_node in active_agents:
        graph.add_edge(agent_node, "fix_agent")

    # --- Edges: fix_agent → sandbox → conditional retry ---
    graph.add_edge("fix_agent", "sandbox_validate")

    graph.add_conditional_edges(
        "sandbox_validate",
        partial(_should_retry, max_attempts=max_attempts),
        {
            "retry": "fix_agent",   # Loop back with failure context
            "done": "reviewer_agent",
        },
    )

    graph.add_edge("reviewer_agent", END)

    compiled = graph.compile()
    logger.info(
        "Graph compiled. Agents: %s, max_fix_attempts: %d", agents, max_attempts
    )
    return compiled


def run_review(
    repo_path: str,
    namespace: str,
    chunks,
    config: SentinelConfig,
    groq_api_key: str,
    e2b_runner=None,
) -> AgentState:
    """Convenience function: build graph, run it, return final state.

    Args:
        repo_path:    Absolute path to the local repo.
        namespace:    Vector store namespace (repo_id).
        chunks:       CodeChunks to analyse.
        config:       Loaded SentinelConfig.
        groq_api_key: From SentinelSecrets.
        e2b_runner:   Optional E2B runner.

    Returns:
        Final AgentState after the full workflow completes.
    """
    graph = build_graph(config, groq_api_key, e2b_runner=e2b_runner)

    initial_state: AgentState = {
        "repo_path": repo_path,
        "namespace": namespace,
        "chunks": chunks,
        "retrieved_chunks": [],
        "findings": [],
        "fixes": [],
        "sandbox_results": [],
        "deduplicated_findings": [],
        "retry_count": 1,
        "current_fix": None,
        "review_score": None,
        "fix_success_rate": None,
        "agent_timeline": [],
    }

    final_state = graph.invoke(initial_state)
    return final_state

# implementation_plan.md — sentinel-review

AI-powered code review engine: LangGraph + Llama 3.3 70B (via Groq) for
bug detection, security analysis, and debugging; Tree-sitter AST +
HuggingFace embeddings + Pinecone (with a local FAISS fallback) for
function-level RAG; E2B sandbox + LangSmith for a self-debugging
feedback loop that validates generated fixes against real runtime output.

Distributed as a pip-installable package with a CLI, a GitHub Action,
a pre-commit hook, and an importable Python API — not a web platform.

**Execution detail lives in `build.md` (ordered tasks) and `skill.md`
(interfaces/conventions to follow). This file is the map; those two are
the territory.**

## Status

- [x] Package scaffold (`pyproject.toml`, `sentinel_review/` layout)
- [x] Tree-sitter AST parser — function/class chunking, docstrings,
      naive call graph (`sentinel_review/ingestion/parser.py`)
- [ ] Everything in `build.md` tasks 1-10

## Architecture

```
sentinel review <repo-or-path> [--diff <ref>] [--auto-fix] [--sarif out]
        |
Ingestion: clone/extract -> Tree-sitter AST parse -> [diff-aware filter
  if --diff] -> content-hash check (skip unchanged) -> embed -> upsert
  to vector store (Pinecone or local FAISS, config-driven)
        |
LangGraph workflow (Groq Llama 3.3 70B, structured Pydantic outputs)
  Bug Agent  ─┐
  Security Agent ─┼─(fan-out, independent)─> Reviewer aggregation
  Docs Agent ─┘
        |
  Fix Agent -> E2B sandbox validation -> [fail: feed stderr back, retry
  up to N] -> [pass + confidence >= threshold + severity allowed:
  auto-apply] -> [else: suggestion only]
        |
Reviewer Agent: deterministic severity/confidence scoring
        |
Report: JSON (canonical) -> SARIF, CLI table output
        |
LangSmith traces throughout -> eval harness computes detection rate /
false-positive rate / fix-success-rate-within-N
```

## Feature set (all in scope, mapped to build.md tasks)

| Feature | build.md task |
|---|---|
| Diff-aware analysis (changed files/functions + call-graph neighbors, not full repo) | Task 4 |
| Confidence-gated auto-fix (only auto-apply if sandbox passes + confidence threshold + severity allowed) | Task 5 (gating.py) |
| Incremental indexing (content-hash cache, skip unchanged embeddings) | Task 3 |
| SARIF output (GitHub Security tab integration) | Task 7 |
| Local vector store fallback (FAISS, zero external accounts to try it) | Task 2 |
| Eval harness (fixture repos with known bugs -> detection/fix-rate numbers) | Task 8 |

## Package layout

```
sentinel-review/
├── pyproject.toml
├── build.md / skill.md / implementation_plan.md
├── sentinel_review/
│   ├── cli.py                      # Task 9
│   ├── config.py                   # Task 1 — .sentinel.yml
│   ├── ingestion/
│   │   ├── parser.py               # done — Tree-sitter AST chunker
│   │   ├── embedder.py             # Task 3
│   │   ├── orchestrator.py         # Task 3 — incremental indexing
│   │   ├── github_loader.py        # Task 4
│   │   ├── zip_loader.py           # Task 4
│   │   └── diff_selector.py        # Task 4 — diff-aware selection
│   ├── vectorstore/
│   │   ├── base.py                 # Task 2 — shared interface
│   │   ├── local_faiss.py          # Task 2
│   │   ├── pinecone_store.py       # Task 2
│   │   └── factory.py              # Task 2
│   ├── graph/
│   │   ├── schemas.py              # Task 5 — Finding, ProposedFix
│   │   ├── state.py                # Task 5
│   │   ├── bug_agent.py            # Task 5
│   │   ├── security_agent.py       # Task 5
│   │   ├── docs_agent.py           # Task 5
│   │   ├── fix_agent.py            # Task 5
│   │   ├── reviewer_agent.py       # Task 5, 7 — scoring
│   │   ├── gating.py               # Task 5 — confidence gate
│   │   └── build_graph.py          # Task 5, 6 — wiring + retry loop
│   ├── sandbox/
│   │   ├── e2b_runner.py           # Task 6
│   │   └── repro.py                # Task 6
│   ├── report/
│   │   ├── sarif.py                # Task 7
│   │   └── json_report.py          # Task 7
│   ├── eval/
│   │   ├── harness.py              # Task 8
│   │   └── fixtures/*/             # Task 8
│   └── integrations/
├── .github/actions/sentinel-review/
│   ├── action.yml                  # Task 10
│   └── Dockerfile
├── .pre-commit-hooks.yaml          # Task 10
├── fixtures/sample.py              # done — parser test fixture
└── tests/
```

## Build order

1. Task 1 (config) — everything else reads from it
2. Task 2 (vector store abstraction) — unblocks local-only dev without Pinecone
3. Task 3 (embedder + incremental indexing)
4. Task 4 (loaders + diff-aware selection)
5. Task 5 (LangGraph agents) — get a **review-only** pipeline working end to end first
6. Task 6 (sandbox + retry loop) — the hardest, most differentiating part
7. Task 7 (scoring + SARIF + JSON report)
8. Task 8 (eval harness) — run it as soon as Task 6 lands, iterate against real numbers
9. Task 9 (CLI) — can actually start earlier in parallel, as a thin shell around stubs
10. Task 10 (GitHub Action, pre-commit) — last, once the CLI is stable

## Environment needed

- Groq API key (LLM)
- Pinecone API key + index (optional — local FAISS works with none)
- E2B API key (sandbox)
- LangSmith API key (tracing — optional but needed for eval harness's
  fix-success-rate metric)

## Definition of done

See the bottom of `build.md` — local-zero-account path working, full
Pinecone+E2B path working, eval harness producing real numbers, SARIF
validating.
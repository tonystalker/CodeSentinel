# CodeSentinel

> AI-powered code review pipeline — LangGraph multi-agent orchestration, Tree-sitter AST RAG, and E2B sandbox self-debugging.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Groq](https://img.shields.io/badge/LLM-Groq%20%7C%20Llama%203.3%2070B-orange)](https://console.groq.com)

---

## What It Does

CodeSentinel reviews pull requests and local repositories for bugs, security vulnerabilities, and documentation gaps — then generates confidence-gated, sandbox-validated fixes. It is designed to run in CI/CD as a GitHub Action or as a developer-local pre-commit hook.

**Key capabilities:**

- 🔍 **Multi-agent review** — `bug_agent`, `security_agent`, and `docs_agent` run in parallel via LangGraph fan-out
- 🧠 **RAG-based context** — Tree-sitter parses code into function/class-level chunks, embedded and retrieved from Pinecone or local FAISS
- 🛠 **Auto-fix with self-debugging** — `fix_agent` generates unified diffs; E2B sandbox validates them and feeds failures back in a retry loop
- 📊 **SARIF output** — results export directly to GitHub's Security tab
- 🔒 **BYOK model** — no maintainer credentials ever bundled; your keys, your billing

---

## High-Level Design (HLD)

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         sentinel review <path>                      │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   Ingestion & Parsing    │
                    │  Tree-sitter AST chunks  │
                    │  (function / class level)│
                    └────────────┬────────────┘
                                 │ CodeChunks
               ┌─────────────────▼─────────────────┐
               │      Incremental Embedding          │
               │  Content-hash dedup → embed only   │
               │  new/changed chunks (SentenceTrans) │
               └─────────────────┬─────────────────┘
                                 │
            ┌────────────────────▼──────────────────────┐
            │          Vector Store (RAG)                │
            │  Pinecone (prod) │ LocalFAISS (zero-setup) │
            │  Diff-aware 1-hop call-graph expansion     │
            └────────────────────┬──────────────────────┘
                                 │ retrieved chunks
     ┌───────────────────────────▼───────────────────────────┐
     │                  LangGraph StateGraph                  │
     │                                                        │
     │   START                                                │
     │     ├──► bug_agent ──────────────────┐                │
     │     ├──► security_agent  (parallel)  │ fan-out        │
     │     └──► docs_agent  ───────────────►│                │
     │                                      │                │
     │                              fan-in  ▼                │
     │                           fix_agent (diffs)           │
     │                                      │                │
     │                           sandbox_validate ◄──retry── │
     │                           (E2B microVM)               │
     │                                      │                │
     │                           reviewer_agent              │
     │                           (dedup + score)             │
     │   END                                                  │
     └───────────────────────────┬───────────────────────────┘
                                 │
               ┌─────────────────▼─────────────────┐
               │          Reporting                  │
               │  SARIF 2.1.0 │ Canonical JSON       │
               │  Rich CLI table │ GitHub Annotations │
               └────────────────────────────────────┘
```

### Data Flow (Step-by-Step)

| Step | Component | What happens |
|------|-----------|--------------|
| 1 | **Ingestion** | Repo loaded from local path, GitHub URL (shallow clone), or ZIP. Tree-sitter parses Python/JS/TS/Java/Go into function + class chunks. |
| 2 | **Embedding** | Each chunk is hashed; only new or modified hashes are embedded with `jina-embeddings-v2-base-code`. Synced to vector store. |
| 3 | **Diff Selection** | Git diff parsed → changed lines identified → chunks overlapping those lines selected + 1-hop call-graph expansion. |
| 4 | **Fan-out Review** | Three agents run in parallel. Each calls the LLM with its system prompt and the retrieved chunks, returning structured `Finding` objects (Pydantic). |
| 5 | **Fix Generation** | `fix_agent` receives all findings, generates unified diffs with calibrated confidence scores per fix. |
| 6 | **Sandbox Validation** | E2B microVM spins up, applies the diff in-process, runs a generated repro script. `passed=True` → auto-apply gate checks confidence + severity. `passed=False` → stderr piped back into `fix_agent` for retry (up to `max_fix_attempts`). |
| 7 | **Dedup + Score** | `reviewer_agent` merges findings overlapping on `(file, rule_id, line range)`, computes weighted severity score. |
| 8 | **Report** | SARIF written for GitHub Security tab; JSON report for downstream tooling; Rich table printed to terminal. |

---

## Tech Stack

| Layer | Choice | Why | Why Not |
|-------|--------|-----|---------|
| **LLM** | Llama 3.3 70B via Groq | Ultra-low latency for multi-agent loops; strong structured JSON output; open-weights (no vendor lock-in) | GPT-4/Claude: closed weights, vendor lock-in; local Ollama: too slow for CI |
| **Orchestration** | LangGraph | Precise control over cyclical graphs (retry loop), deterministic state accumulation via `operator.add` reducers, first-class fan-out/fan-in | LangChain chains: no cycles; AutoGen: too conversational for deterministic CI pipelines |
| **AST Parsing** | Tree-sitter | Semantic function/class boundaries; incremental parsing; supports 6 languages | Regex/RecursiveTextSplitter: break code mid-block, corrupting LLM context |
| **Embeddings** | `jina-embeddings-v2-base-code` | Code-specific model with 8192 token context; strong retrieval on function-level chunks | `text-embedding-3-small`: general-purpose, misses code idioms |
| **Vector Store** | Pinecone (prod) + LocalFAISS (fallback) | Pinecone: managed, serverless, no infra; FAISS: zero-setup for local runs | ChromaDB/Qdrant: extra SQLite/DuckDB deps; added complexity for a fallback path |
| **Sandbox** | E2B Code Interpreter | Fast-booting microVMs designed for AI code execution; clean teardown guarantees | Local Docker: DooD is flaky in GitHub Actions; LLM-generated code + host Docker = security risk |
| **Validation** | Pydantic v2 | `extra="forbid"` catches config typos; `.with_structured_output()` guarantees machine-parseable LLM responses | Dataclasses: no field-level validation; marshmallow: more boilerplate |
| **CLI** | Typer + Rich | Rapid command definition; Rich renders progress spinners and findings tables with no extra work | Click: more boilerplate; argparse: no automatic help generation |

---

## Project Structure

```
CodeSentinel/
├── sentinel_review/
│   ├── config.py           # SentinelConfig (behavior knobs, no secrets)
│   ├── secrets.py          # SentinelSecrets (env vars only, BYOK)
│   ├── cli.py              # Typer CLI: sentinel review / sentinel eval
│   ├── ingestion/
│   │   ├── parser.py       # Tree-sitter AST → CodeChunk list
│   │   ├── embedder.py     # SentenceTransformer, lazy-loaded
│   │   └── orchestrator.py # Incremental sync (content-hash dedup)
│   ├── vectorstore/
│   │   ├── protocol.py     # VectorStore ABC
│   │   ├── local_faiss.py  # NumPy cosine-sim fallback
│   │   └── pinecone_store.py
│   ├── graph/
│   │   ├── schemas.py      # Finding, ProposedFix, SandboxResult, RuleId
│   │   ├── state.py        # AgentState (operator.add fan-out reducers)
│   │   ├── build_graph.py  # LangGraph StateGraph wiring
│   │   ├── bug_agent.py
│   │   ├── security_agent.py
│   │   ├── docs_agent.py
│   │   ├── fix_agent.py
│   │   ├── reviewer_agent.py
│   │   └── gating.py       # Confidence + severity gate for auto-apply
│   ├── sandbox/
│   │   ├── e2b_runner.py   # E2B microVM execution, retry loop
│   │   └── repro.py        # Repro script generation
│   ├── report/
│   │   ├── sarif.py        # SARIF 2.1.0 generator
│   │   └── json_report.py  # Canonical JSON report
│   └── eval/
│       ├── harness.py      # Evaluation harness (bipartite matching)
│       └── fixtures/       # 6 fixture repos with injected bugs
├── tests/                  # 53 unit tests
├── .github/
│   └── workflows/          # GitHub Action
├── .pre-commit-hooks.yaml
├── .sentinel.yml.example
└── .env.example
```

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/tonystalker/CodeSentinel.git
cd CodeSentinel
pip install -e ".[dev]"
```

### 2. Configure API Keys

```bash
cp .env.example .env
# Edit .env and fill in:
# GROQ_API_KEY=gsk_...            (required)
# E2B_API_KEY=...                 (optional — enables sandbox fix validation)
# PINECONE_API_KEY=...            (optional — enables cloud vector store)
# LANGCHAIN_API_KEY=...           (optional — enables LangSmith tracing)
# SENTINEL_LLM_MODEL=llama-3.1-8b-instant  # optional — override default model
#   Useful when hitting Groq's 100k tokens/day limit on llama-3.3-70b-versatile.
#   llama-3.1-8b-instant has a separate 500k TPD quota.
```

> **BYOK model:** All keys are billed to your accounts. The package never bundles the maintainer's credentials. Never commit `.env`.

### 3. Run a Review

```bash
# Scan a local repo
sentinel review /path/to/your/repo

# Scan only files changed since main
sentinel review /path/to/your/repo --diff main

# Scan a GitHub URL
sentinel review https://github.com/owner/repo

# Enable auto-fix (requires E2B_API_KEY)
sentinel review /path/to/your/repo --auto-fix

# Output SARIF for GitHub Security tab
sentinel review /path/to/your/repo --sarif results.sarif
```

### 4. Run the Eval Harness

```bash
sentinel eval

# Pipe results to a file (recommended — rich table truncates in small terminals)
PYTHONIOENCODING=utf-8 python -m sentinel_review.eval.harness > eval_results.txt

# Use a lighter model if the daily token limit is exhausted
SENTINEL_LLM_MODEL=llama-3.1-8b-instant sentinel eval
```

> **Windows users:** Set `$env:PYTHONIOENCODING="utf-8"` in PowerShell before running the harness, or Rich will crash on Unicode table characters.

#### Eval Harness Metrics

| Column | Meaning |
|--------|---------|
| `Expected` | Number of bugs labelled in `expected.yml` |
| `Found` | Total findings returned by the pipeline |
| `TP` | True positives — findings that match an expected entry by `rule_id` + overlapping line range |
| `FP` | False positives — findings matching no expected entry |
| `Dup Reports` | Findings that re-match an already-matched expected entry (same bug reported twice via fan-out) |
| `Detection Rate` | `TP / Expected`, capped at 100% |
| `FP Rate` | `FP / Found` |
| `Fix Passed` | Whether the E2B sandbox validated at least one fix for this fixture (`skipped` if no `E2B_API_KEY`) |

> `Dup Reports > 0` means `reviewer_agent` didn't merge two findings for the same bug — investigate agent fan-out dedup if you see this.

---

## Configuration

Copy `.sentinel.yml.example` to `.sentinel.yml` in your repo root. All fields are optional — missing file uses sane defaults.

```yaml
# .sentinel.yml
vector_store: local          # "local" (FAISS, zero-setup) or "pinecone"
auto_fix: false              # enable confidence-gated auto-apply
auto_fix_confidence_threshold: 0.85
auto_fix_allowed_severities:
  - high
  - medium
  - low                      # "critical" intentionally absent — always human-reviewed
max_fix_attempts: 3
sandbox_timeout_seconds: 90
enabled_agents:
  - bug
  - security
  - docs
llm_model: llama-3.3-70b-versatile  # override via SENTINEL_LLM_MODEL env var
```

---

## GitHub Action

```yaml
# .github/workflows/sentinel.yml
- name: CodeSentinel Review
  uses: tonystalker/CodeSentinel@v1
  with:
    groq_api_key: ${{ secrets.GROQ_API_KEY }}
    e2b_api_key: ${{ secrets.E2B_API_KEY }}    # optional
    auto_fix: false
```

## Pre-commit Hook

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/tonystalker/CodeSentinel
    rev: v0.1.0
    hooks:
      - id: sentinel-review
```

> The pre-commit hook runs only `bug` + `security` agents and skips sandbox validation to stay under 30s.

---

## Rule ID Taxonomy

Agents are constrained to a controlled vocabulary (`RuleId` in `schemas.py`) — agents cannot hallucinate free-form rule IDs. Any finding outside the taxonomy is mapped to `"other"` rather than throwing a validation error.

| Category | Rule IDs |
|----------|----------|
| Secrets & Auth | `hardcoded-secret`, `insecure-default-password` |
| Injection | `sql-injection`, `command-injection`, `path-traversal`, `xss`, `ssrf`, `open-redirect` |
| Crypto | `weak-crypto`, `insecure-random`, `broken-tls` |
| Info Exposure | `sensitive-data-logging`, `info-disclosure` |
| Logic Bugs | `null-deref`, `off-by-one`, `missing-import`, `race-condition`, `unchecked-return`, `type-confusion`, `resource-leak` |
| Access Control | `broken-access-control`, `missing-auth`, `privilege-escalation` |
| Supply Chain | `dependency-confusion`, `insecure-deserialization` |
| Docs | `missing-docstring`, `stale-docstring` |
| Catch-all | `other` |

---

## Design Decisions & Tradeoffs

**LocalFAISS uses NumPy cosine similarity instead of a binary FAISS index.** Sacrifices sub-millisecond query speed on massive codebases but eliminates index-staleness bugs and is fast enough (~2-5ms) for typical repos under 50k chunks.

**In-process diff application instead of GNU `patch` in E2B.** Reduces dependencies on the base E2B image but trades off robust handling of complex multi-file hunks with context lines.

**1-hop call graph expansion in diff selection.** Recursive expansion was rejected — it quickly blows up to the entire repo for deeply nested dependency graphs, wasting tokens and compute.

**Pre-commit hook skips docs agent and sandbox.** Trades fix verification and documentation checks for `< 30s` execution — the threshold beyond which developers start `--no-verify`.

**`critical` severity excluded from auto-apply by default.** Even if the model is highly confident, critical findings always go to a human reviewer. Opt-in via `auto_fix_allowed_severities`.

---

## Known Limitations

- **Groq free tier: 100k tokens/day** on `llama-3.3-70b-versatile`. Running the full eval harness against 6 fixtures consumes the daily budget. Override with `SENTINEL_LLM_MODEL=llama-3.1-8b-instant` (500k TPD) for eval runs.
- **E2B in-process patch** handles single-file unified diffs well; complex multi-file patches with missing context lines may fail to apply cleanly.
- **Tree-sitter chunking** currently extracts top-level functions and classes only — nested functions within a class body are captured as part of the parent class chunk.

---

## Development

```bash
# Run tests
pytest

# Lint
ruff check sentinel_review/

# Type check
mypy sentinel_review/

# Run eval harness (requires GROQ_API_KEY)
PYTHONIOENCODING=utf-8 sentinel eval
```

---

## License

MIT © 2026 Tony Stalker

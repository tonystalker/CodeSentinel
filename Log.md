# sentinel-review Build LOG & High-Level Design (HLD)

---

## 1. High-Level Design (HLD)

**Architecture Overview**
`sentinel-review` is an AI-powered code review pipeline that uses an AST-aware vector store and a LangGraph-orchestrated multi-agent workflow to detect bugs, security vulnerabilities, and documentation issues, followed by confidence-gated auto-fixing using an isolated execution sandbox.

**Core Data Flow:**
1. **Ingestion & Parsing:** Code is extracted (local git, remote GitHub, ZIP) and parsed into semantic chunks (functions, classes) using Tree-sitter.
2. **Incremental Embedding:** A content-hash is computed per chunk. Only new or modified chunks are embedded using a local `SentenceTransformer` and synced to the Vector Store (Pinecone or LocalFAISS).
3. **Diff-aware Selection:** Git diffs are parsed to find changed lines. The chunk graph is queried to select modified chunks plus a 1-hop expansion of direct callers.
4. **Agentic Review (LangGraph):**
   - **Fan-out:** `bug_agent`, `security_agent`, and `docs_agent` analyze the selected chunks in parallel.
   - **Fan-in:** `fix_agent` generates unified diffs (ProposedFixes) for identified findings.
5. **Sandbox Validation:** Fixes are passed to an E2B microVM. A generated reproduction script verifies the fix. If it fails, the error output is fed back into the `fix_agent` in a conditional retry loop.
6. **Reporting:** `reviewer_agent` deduplicates findings and computes severity scores. Results are exported as SARIF (for GitHub Security tab) and Canonical JSON.

---

## 2. Tech Stack Used (Why & Why Not Others)

- **LangGraph** (Orchestration)
  - *Why:* Provides precise, low-level control over cyclical execution (like the retry loop) and allows deterministic state accumulation (fan-out/fan-in) via `AgentState` reducers. 
  - *Why not LangChain chains or AutoGen?* Standard chains lack cyclical graph capabilities. AutoGen is too unconstrained and conversational for a deterministic CI/CD pipeline.
- **Llama 3.3 70B via Groq** (Core LLM)
  - *Why:* Llama 3.3 70B offers state-of-the-art reasoning, and Groq provides the ultra-low latency necessary to run multi-agent, multi-turn validation loops within CI time limits. Strong support for strict structured JSON output.
  - *Why not GPT-4 / Claude?* The spec specifically required open-weights for vendor lock-in avoidance and Groq for speed.
- **Tree-sitter** (Parsing)
  - *Why:* Provides highly accurate, semantic chunking (functions, classes). Semantic boundaries keep logical units intact, drastically improving vector search relevance and LLM context windows.
  - *Why not Regex or RecursiveCharacterTextSplitter?* Character splitters often break code in the middle of blocks, corrupting the semantic meaning for the LLM.
- **Pinecone & LocalFAISS** (Vector Search)
  - *Why:* Pinecone provides a managed, production-grade retrieval backbone. LocalFAISS was built as a lightweight, zero-setup fallback.
  - *Why not ChromaDB or Qdrant?* Pinecone is widely adopted for serverless SaaS. A pure numpy/FAISS fallback avoids dragging in heavy local DB dependencies like SQLite/DuckDB if the user just wants a quick local scan.
- **E2B Code Interpreter** (Sandbox Validation)
  - *Why:* Provides fast-booting, isolated microVMs designed specifically for AI code execution with minimal setup overhead.
  - *Why not local Docker?* Docker-out-of-Docker (DooD) in GitHub Actions is notoriously flaky and poses severe security risks if the LLM generates malicious code. E2B abstracts this away securely.
- **Pydantic** (Data Validation)
  - *Why:* Enforces strict configuration parsing (`extra="forbid"`) and powers the LLM `.with_structured_output()` schema definition, guaranteeing machine-parseable responses.

---

## 3. Tradeoffs Made

- **LocalFAISS Numpy vs Real FAISS Index:** For the local vector store, we compute cosine similarity using NumPy in-memory rather than maintaining a binary `faiss.IndexFlatIP` file. 
  - *Tradeoff:* Sacrifices sub-millisecond query speed on massive codebases, but heavily simplifies state management (avoiding index-staleness bugs) and is plenty fast enough (~2-5ms) for typical repo sizes (< 50k chunks).
- **In-process Diff Application:** We apply unified diffs using a custom Python logic rather than shelling out to GNU `patch` in the E2B sandbox. 
  - *Tradeoff:* Reduces dependencies on the base E2B image but sacrifices robust handling of complex, multi-file hunks with complex context.
- **Pre-commit Hook Speed vs Completeness:** The pre-commit hook runs *only* the bug and security agents (omitting docs) and skips the sandbox validation. 
  - *Tradeoff:* Trades off fix verification and documentation checks for the fast (< 30s) execution time required for a pre-commit hook to not frustrate developers.
- **1-Hop Call Graph Expansion:** Diff selection expands only 1 hop to direct callers. 
  - *Tradeoff:* Recursive expansion was rejected because it quickly blows up to include the entire repo for deeply nested dependency graphs, wasting tokens and compute.

---

## 4. Bottlenecks

- **Vector Embeddings Download:** The initial `SentenceTransformer` instantiation downloads a ~400MB model (`jina-embeddings-v2-base-code`). 
  - *Mitigation:* We lazy-load the model in the `CodeEmbedder` so that CLI `--help` and config commands remain snappy without blocking on network I/O.
- **E2B Package Specificity:** The code strictly requires the `e2b-code-interpreter` package rather than the generic `e2b` package, as the API signatures differ significantly. This is a potential gotcha for developers modifying the `pyproject.toml`.
- **Double-Counting Findings in State:** LangGraph's `operator.add` reducer concatenates fan-out findings. The `reviewer_agent` deduplicates them. Initially, returning them to the same key resulted in double-counting. 
  - *Mitigation:* Addressed by isolating the reviewed output into a separate `deduplicated_findings` key (last-writer-wins) that downstream reporters consume.

---

## 5. Errors Faced & Resolved

- **setuptools build-backend:** `pip install -e .` failed with `BackendUnavailable: Cannot import 'setuptools.backends.legacy'`. 
  - *Root cause:* setuptools >= 70.1 changed the legacy backend path. 
  - *Fix:* Explicitly defined `build-backend = "setuptools.build_meta"` in `pyproject.toml`.
- **LangGraph Fan-in Semantics:** Using `operator.add` caused deduplicated lists to append to raw lists. Addressed by isolating the reviewed output as mentioned in the bottlenecks.
- **PowerShell vs Bash quirks:** The initial test execution via pip used `tail`, which failed in Windows PowerShell. Adapted CLI commands to use `Select-Object -Last` for cross-compatibility during CI/local runs.

---

## 6. Build Log (Chronological summary)

- **2026-07-21:** Specifications read (`implementation_plan.md`, `skill.md`, `build.md`).
- **Task 1:** Built `SentinelConfig` and `SentinelSecrets` (Pydantic). BYOK model enforced (no credentials in yaml).
- **Task 2:** Vector store protocol built. Implemented `LocalFAISS` and `PineconeStore`.
- **Task 3:** `CodeEmbedder` (lazy loading) and `IngestOrchestrator` (incremental sync via content hashing) implemented.
- **Task 4:** GitHub shallow clone loader, ZIP loader (with Zip-slip protection), and diff-selector (1-hop expansion).
- **Task 5:** LangGraph schemas (`Finding`, `ProposedFix`) and agent nodes created. Graph built with parallel fan-out and conditional routing.
- **Task 6:** E2B Runner integrated with isolated `finally` teardown. `repro.py` added to detect existing tests or generate LLM test scripts.
- **Task 7:** Reviewer agent implemented for scoring. SARIF 2.1.0 generator written for GitHub integration.
- **Task 8:** Eval harness created with 6 localized fixtures (missing import, SQL injection, off-by-one, etc.).
- **Task 9:** Typer CLI (`sentinel review`, `sentinel eval`) implemented with Rich progress UI.
- **Task 10:** GitHub Action (`action.yml`, `Dockerfile`, `entrypoint.sh`) and `.pre-commit-hooks.yaml` finalized.
- **Testing:** 53/53 tests passed locally across config, vector store, orchestrator, parser, gating, and SARIF generation.

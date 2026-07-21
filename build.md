# build.md — ordered execution plan

Read `skill.md` first — it defines the interfaces and conventions every
task below must follow. Each task lists the file(s) to create, what it
must expose, and how to know it's done. Do them in order — later tasks
depend on earlier ones.

---

## Task 1 — Config system

**Files:** `sentinel_review/config.py`, `sentinel_review/secrets.py`

- `SentinelConfig` (Pydantic model) in `config.py`, loaded from
  `.sentinel.yml` in repo root, with sane defaults if the file is
  absent. **No credential fields on this model — see `skill.md` section 0.**
  - `vector_store: Literal["pinecone", "local"] = "local"`
  - `auto_fix: bool = False`
  - `auto_fix_confidence_threshold: float = 0.85`
  - `auto_fix_allowed_severities: list[str] = ["high", "medium", "low"]`
  - `max_fix_attempts: int = 3`
  - `ignore_paths: list[str] = []`
  - `enabled_agents: list[str] = ["bug", "security", "docs"]`
  - `sandbox_timeout_seconds: int = 90`
- `load_config(repo_path: Path) -> SentinelConfig`
- `secrets.py`: `SentinelSecrets` (Pydantic model, `groq_api_key: str`
  required, `pinecone_api_key`/`e2b_api_key`/`langchain_api_key:
  str | None = None`), populated from `os.environ` via
  `load_secrets() -> SentinelSecrets`, raises a clear error naming the
  missing var if `GROQ_API_KEY` is absent.
- **Done when:** unit test loads a sample `.sentinel.yml` and confirms
  overrides apply; loading a repo with no config file returns defaults
  without error; a test confirms `.sentinel.yml`'s schema has no field
  that accepts a credential; `load_secrets()` with only `GROQ_API_KEY`
  set returns optional fields as `None` without raising.

## Task 2 — Vector store abstraction

**Files:** `sentinel_review/vectorstore/base.py`,
`sentinel_review/vectorstore/local_faiss.py`,
`sentinel_review/vectorstore/pinecone_store.py`,
`sentinel_review/vectorstore/factory.py`

- Implement the `VectorStore` protocol from `skill.md` section 1.
- `local_faiss.py`: persist to `.sentinel_cache/{repo_id}/`, metadata
  sidecar as JSON (chunk_id -> {content_hash, file_path, code, ...}).
- `pinecone_store.py`: namespace = `repo_id`, metadata per
  `CodeChunk.to_metadata()` plus `content_hash`.
- `factory.py`: `get_vector_store(config: SentinelConfig) -> VectorStore`
- **Done when:** same test suite (upsert 3 chunks, query, delete) passes
  against both backends via a shared pytest fixture parametrized on backend.

## Task 3 — Embedder + content-hash incremental indexing

**Files:** `sentinel_review/ingestion/embedder.py`,
`sentinel_review/ingestion/orchestrator.py`

- `embedder.py`: wraps a code-tuned sentence-transformers model
  (`jinaai/jina-embeddings-v2-base-code` or equivalent), batches chunks,
  returns `list[list[float]]`.
- Add `content_hash: str` to `CodeChunk` in `parser.py` (sha1 of `code`
  field) — small edit, do this first.
- `orchestrator.py`: implements the incremental logic from `skill.md`
  section 3 — diff manifest against current chunk set, embed only
  new/changed chunks, delete stale ones, write updated manifest.
- **Done when:** running orchestrator twice on an unchanged repo makes
  zero embedding calls the second time (assert via a call-count mock on
  the embedder); changing one function only re-embeds that chunk.

## Task 4 — GitHub/ZIP loaders + diff-aware selection

**Files:** `sentinel_review/ingestion/github_loader.py`,
`sentinel_review/ingestion/zip_loader.py`,
`sentinel_review/ingestion/diff_selector.py`

- `github_loader.py`: shallow clone (`depth=1`) via GitPython to a temp
  dir, return local path + repo_id (owner/name).
- `zip_loader.py`: extract with zip-slip protection (validate every
  extracted path stays under the target dir before writing).
- `diff_selector.py`: implements `skill.md` section 4 —
  `get_changed_chunks(repo_path, base_ref, head_ref, all_chunks) -> list[CodeChunk]`,
  including the 1-hop call-graph expansion.
- **Done when:** `diff_selector` on a repo with a 1-line change to a
  helper function returns that function's chunk plus its direct callers,
  and nothing else.

## Task 5 — LangGraph agent workflow

**Files:** `sentinel_review/graph/state.py`, `sentinel_review/graph/schemas.py`,
`sentinel_review/graph/{bug,security,docs,fix,reviewer}_agent.py`,
`sentinel_review/graph/gating.py`, `sentinel_review/graph/build_graph.py`

- `schemas.py`: `Finding`, `ProposedFix` Pydantic models from `skill.md`
  section 2.
- `state.py`: shared `AgentState` TypedDict — repo context, retrieved
  chunks, findings list, fixes list, sandbox results, retry_count.
- Each `*_agent.py`: one function `run(state: AgentState, llm) -> AgentState`,
  uses `.with_structured_output()` per `skill.md` section 2. Bug/security/docs
  agents can run independently (fan-out); reviewer runs last (fan-in).
- `gating.py`: `should_auto_apply()` per `skill.md` section 5.
- `build_graph.py`: wires nodes with `langgraph.StateGraph`, conditional
  edge for severity routing (critical findings prioritized to fix_agent
  first), conditional edge for the retry loop (task 6 plugs in here).
- **Done when:** graph compiles and runs end-to-end on `fixtures/sample.py`
  against a live Groq key, producing at least one `Finding` for the
  missing `json` import bug already planted in that fixture.

## Task 6 — E2B sandbox validation + retry loop

**Files:** `sentinel_review/sandbox/e2b_runner.py`,
`sentinel_review/sandbox/repro.py`

- `e2b_runner.py`: `run_fix(repo_snapshot, fix: ProposedFix, config) -> SandboxResult`
  — fresh sandbox per call (per `skill.md` section 6), install deps,
  apply `fix.diff`, run repro, capture stdout/stderr/exit code, teardown
  in a `finally` block, enforce `config.sandbox_timeout_seconds`.
- `repro.py`: if the target repo has tests covering the changed file, run
  those; otherwise ask the LLM to generate a minimal repro script for the
  specific bug (separate structured-output call, `ReproScript` model with
  a `code` field and `expected_behavior` description).
- Wire the retry loop into `build_graph.py`: on sandbox failure, feed
  `SandboxResult.stderr` back into `fix_agent` as additional context,
  increment `retry_count`, loop until pass or `max_fix_attempts`.
- LangSmith: tag every sandbox run with `attempt_number` + `rule_id`
  (per `skill.md` section 6).
- **Done when:** a fixture with a known one-line bug gets a validated,
  passing fix within 3 attempts, and a fixture with a genuinely hard bug
  exhausts attempts and reports as "suggested, unvalidated" rather than
  crashing the pipeline.

## Task 7 — Reviewer agent + scoring + SARIF report

**Files:** `sentinel_review/graph/reviewer_agent.py` (scoring logic),
`sentinel_review/report/sarif.py`, `sentinel_review/report/json_report.py`

- Scoring is deterministic code, not LLM freehand (per earlier plan) —
  weighted function over finding severities + fix success rate.
- `sarif.py`: `findings_to_sarif(findings) -> dict` per `skill.md` section 7.
  Validate output against the SARIF 2.1.0 JSON schema in a test.
- `json_report.py`: the full structured report (findings, fixes,
  sandbox results, scores) as the canonical output; SARIF and any future
  PDF/HTML views are generated from this, not from agent state directly.
- **Done when:** running against `fixtures/sample.py` produces a valid
  SARIF file that GitHub's `sarif-schema` validator (or a local JSON
  schema check) accepts.

## Task 8 — Eval harness

**Files:** `sentinel_review/eval/harness.py`, `sentinel_review/eval/fixtures/*/`

- 4-6 fixture repos, one bug pattern each (missing import, off-by-one,
  SQL injection via string formatting, unhandled None, race condition on
  shared state, hardcoded secret). Each with `expected.yml`.
- `harness.py`: runs full pipeline per fixture, diffs actual findings
  against `expected.yml`, computes detection rate / false-positive rate
  / fix-success-rate-within-N-attempts, prints a summary table.
- **Done when:** `python -m sentinel_review.eval.harness` runs all
  fixtures and prints the three headline metrics — these are your
  resume numbers.

## Task 9 — CLI

**File:** `sentinel_review/cli.py` (Typer app, matches `pyproject.toml` entry point)

```
sentinel review <path>                  # full scan, local repo
sentinel review <path> --diff <ref>     # diff-aware, vs given base ref
sentinel review <github-url>            # clones then scans
sentinel review <path> --auto-fix       # enable confidence-gated auto-apply
sentinel review <path> --sarif out.sarif
sentinel eval                           # run the eval harness
```

- Rich progress output per agent (reuse `AgentState` transitions to
  drive a live status display — this is your "agent timeline" without
  needing a web frontend).
- **Done when:** all commands above work against a local test repo and
  `fixtures/sample.py`.

## Task 10 — Integration surfaces

**Files:** `.github/actions/sentinel-review/action.yml` + `Dockerfile`,
`.pre-commit-hooks.yaml`

- GitHub Action: thin wrapper, `pip install sentinel-review`, runs
  `sentinel review . --diff ${{ github.base_ref }}`, posts findings as
  PR review comments via the GitHub API, uploads SARIF via
  `github/codeql-action/upload-sarif`.
- Pre-commit hook: runs `sentinel review --staged` (bug/security agents
  only, no sandbox loop — needs to be fast) against staged files.
- **Done when:** action.yml runs successfully in a test workflow against
  this repo itself; pre-commit hook installed locally blocks a commit
  that reintroduces the planted `fixtures/sample.py` bug.

---

## Definition of done for the whole project

- `sentinel review .` works end-to-end with zero external accounts
  (local FAISS + a Groq key) — this is the "try it in 5 minutes" path.
- `sentinel review . --diff main --auto-fix` works with Pinecone + E2B
  configured — this is the "real CI usage" path.
- Eval harness reports concrete detection rate, false-positive rate, and
  fix-success-rate numbers on the fixture set.
- SARIF output validates and (if you wire up the Action against a real
  repo) shows up in GitHub's Security tab.
# skill.md — sentinel-review build conventions

Read this before writing any module in this codebase. It encodes decisions
already made so you don't re-litigate them per-file, and known gotchas for
this specific stack (LangGraph + Groq + Tree-sitter + Pinecone/FAISS + E2B).

## Non-negotiable interfaces

### 0. Secrets vs. config — never mix these

This is a BYOK (bring-your-own-key) tool, not a hosted service. Every
user runs it with their own Groq/Pinecone/E2B/LangSmith credentials,
billed to their own accounts — the package never bundles or transmits
the maintainer's keys.

- `.sentinel.yml` (committed to the user's repo) holds **behavior
  config only**: thresholds, ignore paths, vector store choice, enabled
  agents. It must never contain an API key field, and `config.py`
  should not even define a schema field that could accept one — if
  there's nowhere to put a key, no one can accidentally commit one.
- All credentials (`GROQ_API_KEY`, `PINECONE_API_KEY`, `E2B_API_KEY`,
  `LANGCHAIN_API_KEY`) are read from environment variables / a local
  `.env` (gitignored) via `os.environ`, resolved in a separate
  `sentinel_review/secrets.py`, not in `config.py`.
- Only `GROQ_API_KEY` is required. Missing optional keys degrade
  gracefully: no Pinecone key -> fall back to local FAISS; no E2B key
  -> skip fix validation (fixes reported as unvalidated suggestions
  only, auto-fix gate in section 5 forces `should_auto_apply() = False`
  when no sandbox result exists); no LangSmith key -> tracing is a no-op.
- For the GitHub Action, the key comes from the *adopting repo's* own
  Actions secrets, passed in via `with:` — never a key baked into the
  Action image.

### 1. Vector store abstraction (needed before anything else touches storage)

Every vector backend (Pinecone, FAISS local) implements the same interface
so the rest of the codebase never branches on which one is active:

```python
# sentinel_review/vectorstore/base.py
class VectorStore(Protocol):
    def upsert(self, chunks: list[CodeChunk], embeddings: list[list[float]], namespace: str) -> None: ...
    def query(self, embedding: list[float], namespace: str, top_k: int = 8,
               filter: dict | None = None) -> list[ScoredChunk]: ...
    def delete_namespace(self, namespace: str) -> None: ...
    def get_by_chunk_id(self, chunk_id: str, namespace: str) -> CodeChunk | None: ...
```

- Pinecone impl: one namespace per `{repo_id}` (not per job — namespaces
  persist across runs so incremental indexing has something to diff against).
- FAISS impl: local `.sentinel_cache/{repo_id}/index.faiss` +
  a sidecar JSON for metadata (FAISS itself only stores vectors).
- Selection is config-driven (`vector_store: pinecone | local` in
  `.sentinel.yml`), decided in `vectorstore/factory.py`, never hardcoded
  in a call site.

### 2. Structured LLM output — always Pydantic, never parsed free text

Every agent node returns a Pydantic model via the LLM's structured-output
mode, not a text block you regex out. This matters most for the fix agent
(diff format is sensitive to whitespace) and the confidence gate
(need a real float, not "the model sounds confident").

```python
class Finding(BaseModel):
    file_path: str
    start_line: int
    end_line: int
    severity: Literal["critical", "high", "medium", "low"]
    category: Literal["bug", "security", "docs"]
    description: str
    rule_id: str  # stable slug, e.g. "null-deref", "sql-injection" — SARIF needs this

class ProposedFix(BaseModel):
    finding: Finding
    diff: str  # unified diff format
    confidence: float  # 0.0-1.0, model's own calibration
    reasoning: str
```

### 3. Content-hash caching (incremental indexing)

Chunk id is already derived from `(file_path, name, start_line)` in
`parser.py`. Add a `content_hash` field (sha1 of the chunk's code text) to
`CodeChunk`. On ingest:

```
for each chunk:
    existing = vector_store.get_by_chunk_id(chunk.chunk_id, namespace)
    if existing and existing.content_hash == chunk.content_hash:
        skip embedding — reuse existing vector
    else:
        embed and upsert
```

Deleted functions (chunk_id existed last run, not present this run) must
be explicitly removed from the store — don't just let them go stale.
Track this via a per-namespace manifest (`{chunk_id: content_hash}`)
stored alongside the index, diffed at the start of each ingest run.

### 4. Diff-aware analysis

Don't diff at the file level — diff at the AST node level, or you'll
re-analyze whole files for one-line changes.

```
1. git diff base_ref...head_ref --unified=0  (via GitPython) -> changed line ranges per file
2. parse each changed file with parser.py -> chunks
3. keep chunks whose [start_line, end_line] overlaps any changed range
4. expand: pull each kept chunk's `calls` targets from the call-graph/manifest,
   fetch those chunks too (1-hop only — don't recursively expand, it blows up fast)
5. this expanded set is what gets embedded/queried and passed to the agents
```

Full-repo scan is the same pipeline with step 1 skipped (all chunks are
"changed"). Keep one code path, not two.

### 5. Confidence-gated auto-fix

Gate lives in one place, `graph/gating.py`, not scattered across agents:

```python
def should_auto_apply(fix: ProposedFix, sandbox_result: SandboxResult, config: SentinelConfig) -> bool:
    return (
        sandbox_result.passed
        and fix.confidence >= config.auto_fix_confidence_threshold  # default 0.85
        and fix.finding.severity in config.auto_fix_allowed_severities  # default: not "critical" — flag those for human review even if confident
    )
```

If this returns `False`, the fix is still included in the report/PR
comment as a suggestion with the diff shown — just not applied. Never
silently drop a generated fix.

### 6. E2B sandbox retry loop

Cap at `config.max_fix_attempts` (default 3). Each attempt is a fresh
sandbox (don't reuse — state leakage between attempts corrupts your
signal on whether the fix actually works). Feed the *previous attempt's*
stderr/traceback back into the fix agent as explicit context, not just
"try again." LangSmith: tag each attempt with `attempt_number` and
`finding.rule_id` so you can later query "which rule_ids need >1 attempt
most often" — that's a real insight for your eval writeup.

### 7. SARIF output

SARIF 2.1.0 JSON. The mapping that matters:

- `Finding.rule_id` -> `results[].ruleId` AND must appear once in
  `runs[].tool.driver.rules[]` with a human-readable description
- `Finding.severity` -> `results[].level` : critical/high -> `"error"`,
  medium -> `"warning"`, low -> `"note"` (SARIF only has these three levels)
- `file_path`/`start_line`/`end_line` -> `results[].locations[].physicalLocation`
- Write this as its own pure function (`report/sarif.py`,
  `findings_to_sarif(findings: list[Finding]) -> dict`) — no LLM calls,
  no I/O, easy to unit test against the SARIF schema directly.

### 8. Eval harness

Fixtures live in `eval/fixtures/<case_name>/` — each is a tiny repo with
one or more deliberately injected bugs and a `expected.yml` describing
what should be found (rule_id, file, line range) and whether a fix
should be auto-applicable. Harness runs the full pipeline against each
fixture and reports:

- detection rate (expected findings actually surfaced)
- false positive rate (findings not in `expected.yml`)
- fix success rate within N attempts (from LangSmith trace data)

Keep fixtures small and single-purpose — one bug pattern per fixture,
not a realistic multi-bug repo. Makes failures diagnosable.

## Gotchas specific to this stack

- **tree-sitter-languages is broken** on current tree-sitter (API
  mismatch) — use per-language packages (`tree-sitter-python` etc.)
  directly, as `parser.py` already does. Don't add `tree-sitter-languages`
  back as a dependency.
- **Groq structured output**: use `langchain-groq`'s `.with_structured_output(PydanticModel)`
  — don't hand-roll JSON-mode prompting, it's flakier on 70B than the
  native tool-calling structured output path.
- **E2B sandboxes are billed per second running** — always wrap sandbox
  usage in a context manager that guarantees teardown even on exception,
  and set an explicit timeout per attempt (60-90s is usually enough for
  a unit test run; don't leave it unbounded).
- **Pinecone free tier** has a pod/index size limit — namespace-per-repo
  means you'll want a cleanup policy (delete namespaces for repos not
  analyzed in N days) once you're testing against more than a couple repos.
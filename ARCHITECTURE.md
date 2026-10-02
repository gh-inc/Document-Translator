# Document Translator — Architecture

**Status:** Design v3 — format strategy simplified to the Opaque Metadata pattern. Ready for implementation planning.
**Date:** 2026-09-30

This document captures every architectural decision made before writing code,
with the reasoning behind each. It is the three-way contract between the
assignment brief, the reviewer, and the implementation.

**v2 changelog (design review):** replaced the impossible "zero duplicate
billing" guarantee with an explicit at-least-once/exactly-once boundary (§1.2,
§5.1); removed translated-chunk context from the parallel path (§6); split
Block (semantic unit) from Chunk (execution unit) and named the Document IR
(§4, §5); added chunk leases and chunk-attempt cost records (§5); semantic
cache key (§5); PDF fidelity scoped honestly (§6.6); triage as a persisted
contract with a degradation path (§7); `/healthz` vs `/readyz` (§8, §13).

**v3 changelog (format strategy):** replaced the normalized Document IR with
the **Opaque Metadata** pattern: the core pipeline handles only `seq` +
`source_text`; all format specificity travels as an opaque JSON blob on the
Block and is interpreted solely by that format's extractor/renderer pair.
Renderers re-open the original uploaded file as the canvas (§3, §4, §6).
Rationale and rejected alternatives are recorded in DECISIONS.md §3.

---

## 1. Context

Build a document translation service a team would be willing to operate.

- **Input:** a file + target language(s). **Output:** the same file, translated.
- The LLM is one dependency among several: slow, expensive, occasionally wrong.
- Reviewers **kill and restart containers mid-translation**. Nothing may hang
  forever, be silently lost, or corrupted.
- Hard requirements: real OpenAI API behind an interface (tests must not call
  it), at least one genuine use of the OpenAI Agents SDK with tool calling, an
  MCP server usable from Claude Code / Cursor, ≥2 input formats,
  `docker compose up --build` from a fresh clone, README / PROMPTS.md /
  DECISIONS.md.

### 1.1 Who this is for (product judgment)

Target user: **product and operations teams who need working translations of
business documents (specs, reports, contract drafts) with predictable cost and
zero babysitting** — not professional linguists needing CAT tooling.

Consequences: we optimize for reliability, cost transparency, and format
fidelity; we deliberately do not build translation-memory management UIs,
translator workflows, or review/approval chains.

### 1.2 Our three acceptance criteria

1. **Resilience:** a 50-page PDF translation killed (container `kill -9`) at
   60% resumes after restart from the last persisted chunk and completes.
   **Guarantee boundary (exact words matter):** a *committed* chunk result is
   never translated again — exactly-once at our system boundary. Provider
   *invocations* are at-least-once under ambiguous failures (timeout after the
   provider processed the request): the OpenAI Chat Completions API has no
   client idempotency keys, so duplicate provider cost in that narrow window
   is possible, is **measured** (§5, `chunk_attempts`), and is reported.
2. **Cost discipline:** translating the same document a second time to the
   same language costs ≈ $0 in LLM spend, demonstrably, via the cache-hit
   metric. Repetition *within* a document is also translated once (cache is
   block-level, §5).
3. **Multi-language:** one upload → three target languages in one action, each
   tracked, progressed, and billed independently; a failure in one does not
   affect the others.

### 1.3 Quality metric (one, honest, measured)

Back-translation **chrF** on a fixed sample document: translate EN→DE, then
DE→EN with the same pipeline, compare against the original (reference-based).
We use it as a **coarse automated proxy for information preservation and
pipeline consistency — not as a direct translation-quality metric** — and
report the number in DECISIONS.md.

---

## 2. Key decisions at a glance

| # | Decision | Choice | Rejected alternative / why |
|---|----------|--------|----------------------------|
| 1 | Language & framework | Python 3.12 + FastAPI | Node/TS vs. polyglot: full record in DECISIONS.md §1 |
| 2 | Job orchestration | **DB-backed queue in SQLite (WAL) + chunk-level checkpointing** | Celery+Redis: broker loses unacked tasks on `kill -9`; checkpointing still hand-written; more moving parts in compose |
| 3 | Units of work | **Block = semantic/cache/render unit. Chunk = execution/retry/checkpoint unit. Attempt = cost unit.** | "Chunk is everything": conflates semantics with batching |
| 4 | Agent placement | **Triage stage only** (openai-agents SDK, tool calling), output persisted as a contract | Agents on bulk translation = expensive `if` × N chunks |
| 5 | Bulk translation | Plain parallel completions via `LLMProvider` port, **structured per-block output** | Agent loop per chunk: cost, latency, nondeterminism |
| 6 | MCP transport | FastMCP, streamable-http, own container | stdio: can't run as a compose service reachable from host editors |
| 7 | Frontend | React + Vite + TS SPA, built to static, served by FastAPI | htmx: weaker signal for a full-stack role |
| 8 | Formats | PDF (PyMuPDF) + DOCX (python-docx) via the **Opaque Metadata** pattern — the core sees only text + seq | Normalized layout IR: over-engineering; Markdown bridge: fatal layout loss (DECISIONS.md §3) |
| 9 | Datastore | SQLite WAL on a shared volume | Postgres: extra service, no payoff at this scale (§15) |
| 10 | Default model | `gpt-4o-mini` (env-configurable) | Flagship by default: cost without measured quality need — full record in DECISIONS.md §2 |
| 11 | Parallel context | **Source-side only** (plan + glossary + neighboring source blocks) | Previous-chunk *translation* = serial dependency chain, kills parallelism |

---

## 3. System architecture

One repository, one Docker image, three processes:

```
                 ┌────────────────────────────────────────────┐
                 │              docker compose                │
                 │                                            │
  Browser ──────►│  web (FastAPI)                             │
                 │    REST API · static SPA · SSE · /metrics  │
                 │                                            │
  Claude Code ──►│  mcp (FastMCP, streamable-http :8001)      │
  / Cursor       │                                            │
                 │  worker (python -m app.worker)             │
                 │    claim loop · heartbeat · executor       │
                 │                                            │
                 │  shared volume /data:                      │
                 │    app.db (SQLite, WAL) · uploads/ · out/  │
                 └────────────────────────────────────────────┘
```

- **No Redis, no Postgres.** Fewer services → fewer failure modes during the
  reviewers' restart test. SQLite WAL is sufficient for the assessment's
  single worker process and low write concurrency: LLM calls are parallelized,
  while state mutations remain short transactional writes.
- `web` and `mcp` are **thin doors** over `core`. They may create input
  records (documents, jobs, chunk rows at enqueue); **only the worker performs
  job/chunk execution state transitions.**
- `worker` owns: claim, heartbeat, translation loop, assembly.
- `/data/uploads/{document_id}` keeps the original file for the document's
  whole lifetime. It is not merely an input artifact: **renderers re-open it
  as the canvas** into which translations are placed (§6, step 6) — which is
  what preserves everything the extractor never touched (images, headers,
  embedded fonts, styles) by construction rather than reconstruction.

Pipeline shape:

```
Input file → Extractor → Document IR → Triage Agent → TranslationPlan
           → Blocks grouped into Chunks → parallel translation
           → Validation → Renderer → Output file
```

---

## 4. Layering & the Document IR

```
app/
  core/                  # domain. Imports NOTHING from FastAPI/MCP/OpenAI.
    models.py            # DocumentIR, Block, Job, Chunk, TranslationPlan, enums
    ports.py             # LLMProvider, DocumentExtractor, DocumentRenderer
    services/            # TranslationService, JobService, CacheService, PricingService
  adapters/
    llm/                 # openai_provider.py · fake_provider.py · triage_agent.py
    formats/             # pdf.py (PyMuPDF) · docx.py (python-docx) · registry.py
    persistence/         # sqlite repositories · filesystem storage
  api/                   # FastAPI routers, SSE — thin
  mcp_server/            # FastMCP tools — thin
  worker/                # claim loop, lease/heartbeat, bounded-parallel executor
frontend/                # React + Vite + TS + Tailwind
```

Format handling follows the **Opaque Metadata** pattern instead of a
normalized layout IR:

```python
Block:                        # the only document abstraction the core knows
    id: str
    seq: int                  # reading order; chunking & context use only this
    source_text: str          # the only thing the core translates
    source_hash: str          # cache identity
    format_metadata: dict     # OPAQUE JSON — written by the extractor, read
                              # only by the same format's renderer.
                              # PDF: page + bbox + font size.
                              # DOCX: paragraph/run indices.
```

The contract:

- The core (chunker, worker, LLM provider, cache, queue) operates
  **exclusively on `seq` and `source_text`**. It never parses, inspects, or
  modifies `format_metadata` — it persists the blob and hands it back to the
  renderer. This invariant is pinned by a test that feeds the core
  deliberately malformed metadata and expects translation to be unaffected.
- `Extractor: file → list[Block]` pulls text in reading order and records
  whatever the renderer will need into `format_metadata`.
- `Renderer: original file × (seq → translated_text) → output file` re-opens
  the **original upload as the canvas** and places translations using
  `format_metadata` — so anything the extractor ignored (images, headers,
  styles) survives by construction.
- **Adding a format in 10 minutes** = one module implementing both ports +
  one registry entry. DOCX is the proof the PDF design was not overfit.

Core models reject unexpected top-level fields with `ConfigDict(extra="forbid")`.
This strictness does not apply to nested keys in `Block.format_metadata`, whose
`dict[str, Any]` contents remain opaque and survive serialization unchanged.
Persistence record fields map one-to-one to their table columns: chunk-block
membership lives in `ChunkBlockRecord`, never in `ChunkRecord`. Analysis ports
return `DocumentAnalysisRecord` (including its persisted `created_at`), while
triage and translation use the separate in-memory `TranslationPlan`.
`AttemptOutcome` enumerates the persisted attempt outcomes.

Rejected alternatives (full rationale in DECISIONS.md §3): a universal
Document IR with normalized layout semantics, and a Markdown bridge
(PDF → MD → translate → MD → PDF).

---

## 5. Data model (SQLite, WAL)

**documents** — `id`, `filename` (sanitized), `format`, `size_bytes`,
`page_count`, `storage_path`, `status` (`uploaded|extracted|failed`),
`error_code`, `created_at`

**blocks** — `id`, `document_id`, `seq`, `source_text`, `source_hash`,
`format_metadata` (JSON, **opaque to the core** — §4). `UNIQUE(document_id, seq)`

**document_analyses** — `document_id` (PK), `source_language`, `domain`,
`register`, `terms` (JSON), `warnings` (JSON), `triage_status`
(`ok|degraded`), `created_at` — written **once per document**, shared by all
its jobs (§7).

**jobs** — `id`, `document_id`, `batch_id`, `target_language`, `status`
(`queued → running → assembling → done | completed_with_errors | failed`),
`total_chunks`, `done_chunks`, `model`, `prompt_version`, `glossary` (JSON,
target-language rendering), `tokens_in`, `tokens_out`, `cost_usd`,
`error_code`, `error_detail`, `idempotency_key` (UNIQUE), `lease_owner`,
`lease_expires_at`, `created_at`, `updated_at`

**chunks** — `id`, `job_id`, `seq`, `status` (`pending|inflight|done`),
`lease_owner`, `lease_expires_at`, `created_at`. `UNIQUE(job_id, seq)`

**chunk_blocks** — `chunk_id`, `block_id`, `seq_in_chunk`.
`UNIQUE(chunk_id, seq_in_chunk)` — the grouping of blocks into execution
batches.

**block_translations** (doubles as the translation cache) —
`translation_key`, `block_id`, `translated_text`, `created_at`.
`UNIQUE(translation_key, block_id)` where

```
translation_key = hash(target_language, model, prompt_version, glossary_hash)
```

Combined with the block's own `source_hash` identity, the cache key reflects
**every semantic input** of the translation: source text, source language
(via the analysis + prompt), target language, model, prompt version, and
glossary content. A repeated paragraph — within one document or across jobs —
is translated once. Lookups are `INSERT OR IGNORE` + read-back: even two
concurrent executions of the same block cannot commit twice.

**chunk_attempts** — `id`, `chunk_id`, `attempt_no`, `tokens_in`,
`tokens_out`, `cost_usd`, `latency_ms`, `outcome`
(`ok|retryable_error|fatal_error`), `error_detail`, `created_at`.
`job.cost_usd = SUM(chunk_attempts.cost_usd)` — **all** provider spend,
including retried and ambiguous attempts. This is what makes the retry cost
visible and the "what does one document cost" answer honest.

### 5.1 Guarantees (exactly what we do and do not promise)

The enqueue port exposes only
`create_job_with_chunks(job: JobRecord, chunks: list[ChunkRecord],
chunk_blocks: list[ChunkBlockRecord]) -> None`.
A service initiates this aggregate write; the SQLite repository owns its one
transaction and rolls back the job, all chunks, and join rows on failure. Separate
job/chunk creation methods and a Unit of Work abstraction are unnecessary for
the MVP. The explicit join-record argument keeps table records free of derived
grouping data and includes chunk-block membership in the enqueue transaction
described in “Pipeline”.

Application startup explicitly initializes SQLite with `PRAGMA journal_mode=WAL`
before accepting work. Every new connection executes `journal_mode=WAL`,
`synchronous=NORMAL`, `foreign_keys=ON`, and `busy_timeout=20000` (20 seconds);
connection setup and SQL live in the persistence adapter.

| Boundary | Guarantee | Mechanism |
|---|---|---|
| Enqueue | Idempotent | `idempotency_key` UNIQUE; job+chunks written in one transaction |
| Committed translation | **Exactly-once** | `UNIQUE(translation_key, block_id)` + `INSERT OR IGNORE` |
| Provider invocation | **At-least-once** under ambiguity | No idempotency keys in the Chat Completions API; retries after timeout may double-bill. Measured via `chunk_attempts`, capped per job, reported |
| Job progress | Monotonic, durable | State transitions in short transactions; SSE reads the DB |
| Worker crash | Resume from last committed chunk | Job lease + **chunk leases**: `inflight` with expired lease → `pending` |

### 5.2 State machines

```
Job:     queued ──claim──► running ──all chunks terminal──► assembling
            ▲                │                                  │
            │                │ lease expired → reclaimable      ▼
            │                ▼                    done | completed_with_errors
            └────────── failed (retryable via /retry) ◄──────────┘

Chunk:   pending ──claim──► inflight ──all blocks committed──► done
            ▲                 │
            └──── lease expired (worker died) ─────────────────┘
```

- Job lease: 60 s, heartbeat 10 s — covers `running`/`assembling`.
- Chunk lease: covers one in-flight execution; on worker death, expired
  `inflight` chunks return to `pending` and are re-executed. Blocks already
  committed by that chunk are cache hits — the re-execution costs only the
  genuinely uncommitted remainder.
- **Partial failure policy:** a chunk whose retries are exhausted does not
  kill the job. The job completes as `completed_with_errors`; failed blocks
  render as source text (marked), the error is enumerated in the job payload,
  and `POST /api/jobs/{id}/retry` re-queues exactly the failed chunks.
  `done` means: every block translated.

---

## 6. Pipeline

1. **Upload** — magic bytes (`%PDF-`, `PK\x03\x04`), extension whitelist,
   size cap 50 MB, page cap 400, extracted-text cap; filename sanitized.
   Stored at `/data/uploads/{document_id}`.
2. **Extract (once per document)** — file → `list[Block]`; blocks (text +
   opaque `format_metadata`) persisted; the original file stays at
   `/data/uploads/{document_id}` as the future render canvas.
   **Scanned-PDF detection:** no text layer → `failed(scanned_pdf)` (OCR cut,
   §15).
3. **Triage (once per document, §7)** — agent → `document_analyses` row.
4. **Enqueue (per target language)** — one deterministic completion renders
   the term list into a target-language glossary; blocks grouped into chunks
   (~800–1200 tokens by tiktoken, paragraphs never split); job + chunks +
   chunk_blocks written in **one transaction**; `idempotency_key` dedupes.
5. **Translate loop (worker)** — claim job (atomic `UPDATE...RETURNING`),
   heartbeat; claim pending chunks with bounded parallelism
   (asyncio semaphore = 8). Per chunk:
   - collect its blocks lacking a `block_translations` row for this job's key
     (cache lookup first — re-executions and repeated paragraphs are free);
   - LLM call: system prompt with `TranslationPlan` (domain, register) +
     glossary + **source-side context only**: previous/next *source* blocks
     (all known before any translation starts → zero inter-chunk
     dependencies → honest parallelism);
   - structured output: one translation per block id; persist each via
     `INSERT OR IGNORE` + one `chunk_attempts` row, **same transaction**;
   - retry: exponential backoff + jitter, ≤4 attempts on 429/5xx/timeout;
     400/context-length → fatal, chunk exhausts (job continues, §5.2);
   - per-job **soft cost cap** (`MAX_COST_PER_JOB_USD`, default $2.00): a local
     lock reserves estimated spend before each invocation. A rejected call
     exhausts its chunk with `cost_cap_exceeded`; committed work is preserved.
     Assembly derives `completed_with_errors` from missing cache rows, as
     specified by the approved Stage 4 worker plan. Billed attempt usage is
     persisted; unknown transport usage remains unknown.
6. **Assemble & render** — the format's renderer re-opens the original file
   from `/data/uploads/{document_id}` and places each block's translation
   using its `format_metadata` → output file → final job status.

Stage 4 processes one job at a time, with bounded chunk tasks within that job.
An internal persistence coordinator serializes reads and short transactions on
the worker connection; no provider call or rendering operation spans a database
transaction. Checkpoints atomically persist cache entries, attempt accounting,
chunk completion, and progress. Ownership is checked before checkpointing.
Heartbeats renew job and active chunk leases, continuing through assembly.
On cancellation, child tasks are cancelled and awaited; a new worker recovers
expired leases and skips committed translations. SIGTERM/SIGINT stop new claims
while the current job finishes.

** seam coherence without serialization:** terminology and register come from
the persisted plan + glossary (identical for every chunk); local coherence
comes from neighboring *source* context. A previous-chunk-*translation*
context is explicitly rejected: it would make chunk N depend on chunk N−1's
result and turn the parallel map into a serial chain. (A sequential
"polish pass" using translated context is a possible future enhancement —
cut, §15.)

### 6.6 Supported PDF fidelity (stated, not implied)

MVP renders translated text into the original block positions with basic
typography (font size auto-shrink bounded by a minimum readable size; blocks
that cannot fit fall back to clean regenerated pages). **Text-oriented PDFs
come out well; pixel-perfect preservation is explicitly out of scope** —
columns, complex tables, RTL, and heavy reflow are known limits, listed in
DECISIONS.md. DOCX uses top-level paragraph blocks: it clears all inline
content in translated paragraphs, inserts one plain run, and preserves
paragraph styles/properties. Table cells, headers, and footers remain on the
original canvas unchanged; their translation is outside the Stage 3 scope.
The bbox-insertion approach is validated on the sample document
before we commit to it as the default renderer (§17).

Stage 3 implements both format pairs behind the existing ports. Extraction and
rendering run in worker threads. The registry reads at most 2048 bytes in a
thread, checks whitelisted extensions against signatures, and accepts
extensionless stored uploads by signature. The ZIP signature is a routing hint;
the DOCX extractor validates the package and maps failures to `corrupt_file`.
Missing/unsupported files resolve to no adapter. Render failures use
`render_failed`; raw library exceptions are suppressed. PDF fallback counters
are local to each render and logged as `pdf_render_completed` with
`fallback_count` (blocks) and `fallback_pages_count` (appended pages), without
changing the renderer's `Path` return contract. Measurement and limits are
recorded in DECISIONS.md under “Stage 3 format measurements”.

---

## 7. LLM provider & the agent question

```python
class LLMProvider(Protocol):
    async def translate_chunk(self, req: ChunkRequest) -> ChunkResult:
        """-> per-block translations, tokens_in, tokens_out, model"""
```

- `OpenAIProvider` — real API, model from env (`OPENAI_MODEL`, default
  `gpt-4o-mini`).
- `FakeProvider` — deterministic pseudo-translation (`"[de] <text>"`) +
  env-injected failures (`FAKE_FAIL_RATE`, `FAKE_LATENCY_MS`,
  `FAKE_FAIL_MODE=429|500|timeout`). **The entire test suite — retries,
  resume, chaos — runs without spending a cent.** A `@pytest.mark.live`
  suite against the real API is opt-in.

Stage 2 adapters implement this port. `ChunkRequest.context_before` and
`context_after` default to independent empty lists and carry source blocks only.
The OpenAI wire schema is a strict list of block-ID/text pairs, converted into
the existing `ChunkResult.translations` mapping after validating the exact
requested ID set and rejecting duplicates. Context blocks and opaque metadata
are never returned as translations or interpreted by the provider.

Provider failures use the safe catalog in `core/errors.py`. Retry decisions are
independent of SDK exceptions; known usage remains attached to invalid-response
errors for future attempt accounting. Usage after an ambiguous transport failure
is unknown. SDK automatic retries are disabled: the worker owns retries and
records each attempt. `ModelCostCalculator` uses the Stage 2 plan's explicit
pricing snapshot; changes to provider prices require a table update.

**Where the agent earns its keep — triage.** The document is unknown; someone
must look inside it with tools (`get_text_sample`, `detect_language`,
`classify_domain`, `extract_terminology`) and make a judgment shaping all
downstream chunks. Tool calling is real: the agent decides how many samples
to pull and whether to look again.

**The agent's output is a persisted, deterministic contract:**

```
TranslationPlan := document_analyses row {
    source_language, domain, register, terms[], warnings[], triage_status
}
```

After persistence, the bulk pipeline reads the plan and **never touches the
agent runtime** — agent latency and flakiness are quarantined to one
pre-processing step. If triage itself fails after retries, the job is not
blocked: we degrade to a heuristic plan (language via statistical detection,
empty glossary, `triage_status=degraded`, warning surfaced in API/UI).
The agent is an enhancement, not a single point of failure.

**Where an agent would be an expensive `if`:** bulk translation. Mapping a
fixed prompt over N chunks needs determinism, parallelism, and predictable
cost — exactly what an agent loop destroys. The README states this in exactly
these terms; it is one of the brief's explicit questions.

---

## 8. REST API surface

| Endpoint | Purpose |
|---|---|
| `POST /api/documents` | multipart upload → document + block preview |
| `POST /api/jobs` | `{document_id, target_languages[], idempotency_key}` → batch of jobs |
| `GET /api/jobs/{id}` | status, progress, cost, structured error |
| `POST /api/jobs/{id}/retry` | re-queue failed chunks (optional raised cost cap) |
| `GET /api/jobs/{id}/events` | SSE progress stream |
| `GET /api/jobs/{id}/download` | translated file |
| `GET /api/batches/{id}` | all jobs of a multi-language batch |
| `GET /healthz` | liveness: process is up |
| `GET /readyz` | readiness: DB reachable, storage writable, worker heartbeat fresh |
| `GET /metrics` | Prometheus |

Errors are structured: `{error_code, message, retryable}` — never bare
"Something went wrong".

---

## 9. MCP server

FastMCP, streamable-http, `:8001`. Tool surface designed for editor workflow —
**not** one tool per REST endpoint:

| Tool | Returns | Why it exists |
|---|---|---|
| `translate_file(path, target_languages[])` | job ids immediately | Minutes-long work must not block an MCP call |
| `check_status(job_id)` | progress / error / cost | Polling is the client's job |
| `download_result(job_id, output_dir)` | saved file path | Editor-side deliverable |
| `list_recent_jobs(limit)` | recent jobs | "What did I translate yesterday?" |

README ships the exact Claude Code config
(`claude mcp add --transport http stark-translate http://localhost:8001/mcp`)
and a three-step verification recipe.

---

## 10. Frontend

React + Vite + TS + Tailwind, branded for **Stark** (colors/logo lifted from
getstark.co at implementation time). Three views:

1. **Upload** — drag-n-drop, client-side pre-validation, server errors
   rendered specifically ("This PDF has no text layer (scanned document)",
   "422 pages exceeds the 400-page limit").
2. **Job view** — one card per target language, chunk-level progress bar via
   SSE, live cost counter; `completed_with_errors` renders distinctly
   ("3 of 412 blocks could not be translated — kept in English. Retry.");
   failure states carry a concrete action.
3. **History** — past jobs, status filter, re-download.

---

## 11. Failure handling matrix

| Failure | Behavior |
|---|---|
| Corrupt PDF | Magic-byte + open failure → `corrupt_file`, HTTP 422, no job created |
| Scanned PDF | No text layer → `scanned_pdf`, explains OCR is unsupported |
| >400 pages / >50 MB | Rejected at upload: `page_limit` / `size_limit` |
| Unsupported type | `unsupported_format` with the list of supported types |
| Provider 429/5xx/timeout | Per-chunk retry, exp backoff + jitter, ≤4 attempts; exhausted → chunk fails, job continues → `completed_with_errors` |
| Provider 400 / context length | Fatal for the chunk immediately (not retryable), surfaced distinctly |
| Ambiguous timeout (provider may have processed) | Retried → at-least-once billing possible; recorded in `chunk_attempts`, counted in cost metrics |
| `kill -9` mid-translation | Job + chunk leases expire → reclaim → resume from last committed chunk; committed blocks are cache hits, never re-sent |
| Double submit (same key) | Second `POST /api/jobs` returns the existing job (`idempotency_key` UNIQUE) |
| Triage agent failure | Heuristic degraded plan, `triage_status=degraded`, job proceeds |
| Cost runaway | Per-job cap → `cost_cap_exceeded`, committed work preserved |
| Path traversal in filename | Sanitized at upload |
| DOCX zip-bomb | Size cap + extracted-text cap |

---

## 12. Testing strategy

- **Unit:** chunker (paragraph integrity, token bounds), cache key
  construction, job/chunk state machines, pricing table, the opaque-metadata
  invariant (core passes malformed `format_metadata` through untouched).
- **Contract:** one port test-kit run against both `FakeProvider` and
  `OpenAIProvider` (the latter behind `live`).
- **Integration (FakeProvider):** full E2E — upload → job → done → output
  file parses and contains every block translated.
- **Chaos:** start job → cancel worker mid-flight + expire leases → new
  worker → job reaches `done`. Assertions: **no block with a committed
  translation is ever re-requested from the provider** (FakeProvider counts
  invocations per block); duplicate spend, if any, is attributable solely to
  in-flight ambiguous attempts and visible in `chunk_attempts`. Plus
  `scripts/chaos-restart.sh` running the same scenario against real compose —
  the reviewers' exact test.
- **Regression pins:** corrupt file, oversized file, duplicate idempotency
  key, unsupported type, retry-after-partial-failure — each a test that fails
  if behavior regresses.

---

## 13. Observability

- **Logs:** structlog JSON; `job_id`, `chunk_id`, `request_id` on every line;
  worker logs claim/heartbeat/retry/commit events.
- **Metrics (`/metrics`):** chunk LLM latency histogram, job duration
  histogram, `llm_errors_total{code}`, `cache_hits_total`,
  `llm_cost_usd_total`, `llm_cost_retry_share` (spend in attempts after the
  first), `jobs_by_status` gauge, worker heartbeat age.
- **Health:** `/healthz` (liveness) and `/readyz` (DB, storage, worker
  heartbeat) — not conflated.
- **Runbook:** README section "3 a.m." — what to look at first, mapped to the
  metrics above.

---

## 14. Cost & performance measurement plan

- Pricing table per model; every provider call recorded in `chunk_attempts`;
  `job.cost_usd` is a sum over reality, not an estimate.
- DECISIONS.md reports, measured on a fixed sample document: **cost per
  document** (and what dominates it — including the retry share), **p95 chunk
  latency**, **p95 job latency** (before/after enabling chunk parallelism —
  numbers, not adjectives), and the chrF proxy score (§1.3).

---

## 15. Conscious cuts (seed for DECISIONS.md)

| Cut | Why | Cost of cutting |
|---|---|---|
| OCR for scanned PDFs | Tesseract/vision pipeline is a project of its own | Scanned docs rejected with a clear error |
| Pixel-perfect PDF layout | Reflow/overflow handling is unbounded | Text-oriented PDFs only, stated in §6.6 |
| Horizontal worker scaling | Single worker with bounded async concurrency is sufficient for the assessment workload — **measured in §14, not asserted** | Scaling out would require stronger queue/claiming semantics (real broker); stated, not built |
| Auth / multi-tenancy | Out of scope for an assessment | Anyone on the network can use it — fine for local compose |
| Glossary editing UI | Triage glossary is automatic | User can't override terminology |
| Sequential polish pass w/ translated context | Time; plan+glossary coherence covers the common case | Style seams possible between distant chunks |
| Side-by-side preview / editing | Time | Download-only output |

---

## 16. Repository layout & delivery

```
├── ARCHITECTURE.md (this file)
├── README.md            # quickstart, architecture summary, testing guide, 3am runbook
├── DECISIONS.md         # cuts, trade-offs, measured cost/p95, 3-more-weeks plan
├── PROMPTS.md           # honest AI-usage log incl. rejected output
├── docker-compose.yml   # web · worker · mcp + shared volume
├── Dockerfile           # multi-stage: node build → python runtime
├── .env.example         # OPENAI_API_KEY, OPENAI_MODEL, caps, fake-provider flags
├── app/                 # backend (§4)
├── frontend/            # React SPA
├── tests/
└── scripts/             # chaos-restart.sh, sample docs generator
```

`docker compose up --build` from a fresh clone must be the only setup step.

---

## 17. Open questions to resolve during implementation

1. Exact Stark brand palette (fetch getstark.co at build time).
2. PDF bbox insertion: measure overflow/fallback rate on the sample doc; if
   ugly, flip default rendering to clean-regen and say so (§6.6).
3. Final default model: run the sample doc through `gpt-4o-mini` and one
   stronger model; keep the cheaper one unless chrF justifies otherwise.
4. Glossary rendering quality: one deterministic completion per language —
   verify term consistency on the sample doc; if poor, revisit.

---

## 18. Requirements traceability

This section maps the assessment brief's hard requirements and evaluation
criteria to the concrete design decisions in this document. It exists so a
reviewer can verify that no hard requirement was silently dropped.

### 18.1 Hard requirements from the brief

| Requirement | Design decision | Section |
|---|---|---|
| Web interface accepts PDF uploads | FastAPI `POST /api/documents` + React upload view | §8, §10 |
| Translate content with an LLM | `LLMProvider` port; bulk translation via parallel completions | §7 |
| Output a translated PDF | Format renderer re-opens the original file as the canvas | §4, §6 |
| Survive `kill -9` mid-translation | SQLite WAL + chunk leases + chunk-level checkpointing | §1.2, §2, §5.1, §5.2, §12 |
| Backend language/framework free choice | Python 3.12 + FastAPI | §2 |
| Real OpenAI API behind an interface; tests avoid it | `LLMProvider` Protocol with `OpenAIProvider` and `FakeProvider`; `@pytest.mark.live` | §7, §12 |
| OpenAI Agents SDK with tool calling used where it earns its keep | Triage stage only; output persisted as `TranslationPlan` | §7 |
| MCP server usable from Claude Code / Cursor | FastMCP streamable-http on `:8001`; README ships exact config | §6, §9 |
| At least two input formats | PDF + DOCX via the Opaque Metadata pattern | §2, §4, §6.6 |
| Frontend free choice | React + Vite + TypeScript SPA served by FastAPI | §7, §10 |
| `docker compose up --build` works from a fresh clone | Single Docker image, three processes, shared `/data` volume | §3, §16 |
| `README.md`: architecture, decisions, testing guide | Required deliverable; includes quickstart, testing guide, 3 a.m. runbook | §16 |
| `PROMPTS.md`: AI-usage log incl. rejected output | Required deliverable; maintained as work proceeds | §16 |
| `DECISIONS.md`: cuts, trade-offs, measured cost/p95, 3 more weeks | Required deliverable; measured numbers filled at implementation end | §16 |

### 18.2 Evaluation criteria from the brief

| Criterion | How the design addresses it | Section |
|---|---|---|
| **Agency / product judgment** | Explicit target user and three acceptance criteria written by the team | §1.1, §1.2 |
| **Engineering fundamentals — data model & API** | Document IR, job/chunk/attempt model, REST surface | §4, §5, §8 |
| **Engineering fundamentals — layering** | `core/` is independent; `api/` and `mcp_server/` are thin doors | §3, §4 |
| **Engineering fundamentals — concurrency** | Bounded async parallelism (semaphore 8); short DB transactions | §5.2, §6 |
| **Engineering fundamentals — idempotency & retries** | `idempotency_key`; exactly-once cache; at-least-once invocations measured | §5.1, §11 |
| **Engineering fundamentals — failure handling** | Failure matrix covering corrupt, scanned, oversized, provider errors, crashes | §11 |
| **Engineering fundamentals — tests** | Unit, contract, integration, chaos, regression pins | §12 |
| **Engineering fundamentals — observability** | Structured logs, Prometheus `/metrics`, `/healthz`, `/readyz`, 3 a.m. runbook | §13 |
| **Product judgment — quality metric** | Back-translation chrF reported as an honest proxy; reference-based FLORES-style metric and number/placeholder preservation are *deferred* | §1.3, §14 |
| **AI leverage** | `PROMPTS.md` logs delegation, rejection, and correction | §16 |
| **UX — Stark branding** | React UI branded from getstark.co | §10 |
| **UX — clear feedback during translation** | SSE progress stream, live cost, explicit error states | §8, §10 |

### 18.3 Deferred or rejected items

The following items from the brief are intentionally not built into the MVP.
They are either cuts (with rationale in `DECISIONS.md`) or deferred to a
future quality-metric pass:

- OCR for scanned PDFs — rejected with explicit `scanned_pdf` error.
- Pixel-perfect PDF layout for complex tables / RTL — scoped down to
  text-oriented PDFs.
- Horizontal worker scaling — single worker + bounded concurrency is
  sufficient for the assessment workload.
- Side-by-side preview / in-place editing — cut for time.
- Reference-based FLORES-style chrF and number/placeholder preservation
  metrics — **deferred**; current plan uses back-translation chrF as a proxy.
  If time allows, add a reference-based sample set and a preservation metric
  and report both numbers in `DECISIONS.md`.

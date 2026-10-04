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
   is possible. Attempt and job cost totals include only usage reported by the
   provider and estimate-priced by the application; usage not returned after
   an ambiguous failure is unknown and excluded from those totals.
2. **Cost discipline:** byte-identical uploads and matching job submissions
   are idempotent. Edited documents reuse durable translations for exact source
   text under the same language, model, prompt version, glossary and entire
   analysis plan. Changed plans invalidate reuse. Repeated paragraphs reuse
   committed rows; simultaneous misses can still invoke the provider more than
   once. A cold cache has no hits; measure hit rate over a window. Durable
   job-level lookup counts expose reuse without claiming measured live savings.
   Job costs cover reported chunk usage; triage is recorded separately and
   unknown provider usage remains excluded.
3. **Multi-language:** one upload → three target languages in one action, each
   tracked, progressed, and billed independently; a failure in one does not
   affect the others.

### 1.3 Quality metric (one, honest, measured)

**chrF** is computed by the standard-library-only `app/core/quality.py`,
with unit tests pinning character n-gram orders 1–6, beta=2, effective-order
averaging, and whitespace exclusion. Number, date, currency and placeholder
preservation compares exact literal multisets, counting repeated occurrences.

The reference baseline uses a licensed 20-pair FLORES-200 EN-DE devtest subset,
with disclosed identical synthetic literal suffixes for preservation coverage.
`measure_quality --models` compares models using the same reference and a fresh
private database/cache per model. This is a narrow extracted-text comparison,
not a representative corpus score or a document-fidelity benchmark; synthetic
suffixes can slightly inflate chrF. Provenance and CC BY-SA terms are in
[samples/golden_dataset.md](samples/golden_dataset.md); measured results are in
DECISIONS.md.

Without a supplied reference, the same CLI runs back-translation EN→DE→EN and
labels it as a coarse proxy for pipeline consistency and information retention,
not direct translation quality. Both modes retain their explicit scope.

---

## 2. Key decisions at a glance

| # | Decision | Choice | Rejected alternative / why |
|---|----------|--------|----------------------------|
| 1 | Language & framework | Python 3.12 + FastAPI | Node/TS vs. polyglot: full record in DECISIONS.md §1 |
| 2 | Job orchestration | **DB-backed queue in SQLite (WAL) + chunk-level checkpointing** | Celery+Redis: broker loses unacked tasks on `kill -9`; checkpointing still hand-written; more moving parts in compose |
| 3 | Units of work | **Block = semantic/cache/render unit. Chunk = execution/retry/checkpoint unit. Attempt = provider-usage accounting unit.** | "Chunk is everything": conflates semantics with batching |
| 4 | Agent placement | **Triage stage only** (openai-agents SDK, tool calling), output persisted as a contract | Agents on bulk translation = expensive `if` × N chunks |
| 5 | Bulk translation | Plain parallel completions via `LLMProvider` port, **structured per-block output** | Agent loop per chunk: cost, latency, nondeterminism |
| 6 | MCP transport | FastMCP, streamable-http, own container | stdio: can't run as a compose service reachable from host editors |
| 7 | Frontend | React + Vite + TS SPA, built to static, served by FastAPI | htmx: weaker signal for a full-stack role |
| 8 | Formats | PDF (PyMuPDF) + DOCX (python-docx) + Markdown via the **Opaque Metadata** pattern — the core sees only text + seq | Normalized layout IR: over-engineering; Markdown bridge: fatal layout loss (DECISIONS.md §3) |
| 9 | Datastore | SQLite WAL on a shared volume | Postgres: extra service, no payoff at this scale (§15) |
| 10 | Default model | `gpt-4o-mini` (env-configurable) | A small reference baseline compares both models; it does not justify changing the deployment default — DECISIONS.md |
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
    formats/             # pdf.py · docx.py · markdown.py · registry.py
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
                              # DOCX: body/table paragraph locators.
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
- Markdown is the third supported format. Tables require leading and trailing
  outer pipes; unpiped GFM tables are outside this adapter's supported grammar.
  Its table delimiters and padding
  stay in opaque metadata; model output supplies cell text only. Empty cells
  remain real blocks for rendering, but adapter-classified IDs bypass chunking,
  bulk provider calls and the cache. Markdown tables and paragraphs in top-level
  DOCX table cells are translated. DOCX nested tables, headers and footers
  remain unchanged.

Empty-cell bypass preserves bulk translation token usage and cost for the
same translated content. Triage still receives the full document IR for
navigation; live triage expense may vary and is not claimed invariant.

Adding a format requires coordinated changes across these touchpoints:

| Touchpoint | Location |
|---|---|
| Format allow-list | `app/core/services/document_service.py` |
| Signature or UTF-8 text detection | `app/adapters/formats/registry.py` |
| Extractor and renderer | `app/adapters/formats/<format>.py` |
| REST composition | `app/api/dependencies.py` |
| MCP composition | `app/mcp_server/runtime.py` |
| Worker composition | `app/worker/__main__.py` |
| Browser accept list and MIME validation | `frontend/src/features/upload/UploadForm.tsx` |
| Download extension inference | `frontend/src/features/jobs/JobCard.tsx` |

The original six-touchpoint audit grouped composition as one entry and omitted
MCP and worker wiring. Format-specific bypass also needs a service callback,
renderer completion reporting, tests and documentation. No time estimate is
claimed.

Core models reject unexpected top-level fields with `ConfigDict(extra="forbid")`.
This strictness does not apply to nested keys in `Block.format_metadata`, whose
`dict[str, Any]` contents remain opaque and survive serialization unchanged.
Persistence record fields map one-to-one to their table columns: chunk-block
membership lives in `ChunkBlockRecord`, never in `ChunkRecord`. Analysis ports
return `DocumentAnalysisRecord` (including its persisted `created_at`), while
triage and translation use the separate in-memory `TranslationPlan`.
`AttemptOutcome` enumerates the persisted attempt outcomes.

Rendering returns the in-memory `RenderResult`: output path, degraded block IDs,
passthrough block IDs, and fallback block/page counts. `DocumentIR.warnings`
and the named service `UploadResult` carry extraction warnings to the upload response; these are not
persistence records and add no database columns.

Rejected alternatives (full rationale in DECISIONS.md §3): a universal
Document IR with normalized layout semantics, and a Markdown bridge
(PDF → MD → translate → MD → PDF).

---

## 5. Data model (SQLite, WAL)

**documents** — `id`, `filename` (sanitized), `format`, `size_bytes`,
`page_count`, `storage_path`, `status` (`uploaded|analyzing|extracted|failed`),
`error_code`, `created_at`

**blocks** — `id`, `document_id`, `seq`, `source_text`, `source_hash`,
`format_metadata` (JSON, **opaque to the core** — §4). `UNIQUE(document_id, seq)`

**document_analyses** — `document_id` (PK), `source_language`, `domain`,
`register`, `terms` (JSON), `warnings` (JSON), `triage_status`
(`ok|degraded`), `created_at`, `tokens_in`, `tokens_out`, `cost_usd`,
`cost_usd_total`, `tokens_in_total`, `tokens_out_total` — shared by all jobs
for the document. Current-plan usage is overwritten on re-triage; running totals
include every reported attempt, including failures and retries. Degraded
delete-and-republish carries the totals forward in the same transaction (§7).

**jobs** — `id`, `document_id`, `batch_id`, `target_language`, `status`
(`queued → running → assembling → done | completed_with_errors | failed`),
`total_chunks`, `done_chunks`, `cache_hit_blocks`, `cache_miss_blocks`,
`model`, `prompt_version`, `glossary` (JSON,
target-language rendering), `tokens_in`, `tokens_out`, `cost_usd`,
`error_code`, `error_detail`, `idempotency_key` (UNIQUE), `lease_owner`,
`lease_expires_at`, `created_at`, `updated_at`

**chunks** — `id`, `job_id`, `seq`, `status` (`pending|inflight|done`),
`lease_owner`, `lease_expires_at`, `created_at`. `UNIQUE(job_id, seq)`

**chunk_blocks** — `chunk_id`, `block_id`, `seq_in_chunk`.
`UNIQUE(chunk_id, seq_in_chunk)` — the grouping of blocks into execution
batches.

**block_translations** (the content-addressed translation cache) —
`translation_key`, `source_hash`, `translated_text`, `created_at`.
`PRIMARY KEY(translation_key, source_hash)` where

```
translation_key = hash(target_language, model, prompt_version, glossary_hash, plan_hash)
```

The shared core cache-key module hashes canonical JSON. `plan_hash` covers the
entire rendered `TranslationPlan`, including source language, domain, register,
terms, warnings and triage status; list order is preserved. Source text enters
through the block's existing SHA-256 `source_hash`. Document-scoped `block_id`
is excluded: `blocks.id` is globally unique and the provider needs distinct IDs
within a request, even for identical text. Cache reads and writes use source
hashes; rendering still uses each document's original block IDs.

`INSERT OR IGNORE` commits one row per cache identity. Only durable rows skip
provider calls: duplicate text within one request or concurrent uncached chunks
can still be sent twice. Neighbouring source context is deliberately excluded
from the key, so unchanged text may reuse a translation produced under different
context. Legacy block-ID cache rows are dropped during migration because their
old keys omit the plan. Done chunk statuses remain; an in-flight job may finish
with source-text fallbacks and require manual Retry to retranslate cache misses.

Job `cache_hit_blocks` and `cache_miss_blocks` count durable lookup observations
in a short transaction before provider work. They default to zero and increment
atomically across chunk tasks. Re-delivery or manual retry may observe and count
a block again; a crash before the counter transaction can undercount observations.
The counters measure lookups, not unique blocks or avoided provider billing.

**chunk_attempts** — `id`, `chunk_id`, `attempt_no`, `tokens_in`,
`tokens_out`, `cost_usd`, `latency_ms`, `outcome`
(`ok|retryable_error|fatal_error`), `error_detail`, `created_at`.
`job.cost_usd = SUM(chunk_attempts.cost_usd)` — the sum of application estimates
for provider-reported usage persisted on attempt rows. It includes known usage
from retries, but is not total provider billing: triage usage is recorded
separately on `document_analyses`, never multiplied by the language-job count;
a transport failure can leave usage unknown; and a worker killed before recording
an outcome may leave no attempt row. Triage is excluded from job totals but
included in document expense; unknown or uncheckpointed usage remains excluded
from both.

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
| Committed translation | **Exactly-once** | `PRIMARY KEY(translation_key, source_hash)` + `INSERT OR IGNORE` |
| Provider invocation | **At-least-once** under ambiguity | No client idempotency key; retries after timeout may be billed twice. Attempt rows and cost totals include reported usage when available; unreturned usage is unknown and excluded, and a process interruption may leave no attempt row. Triage usage is outside `chunk_attempts`. |
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

1. **Upload** — binary magic bytes (`%PDF-`, `PK\x03\x04`) or Markdown
   UTF-8/NUL validation, extension whitelist,
   size cap 50 MiB, page cap 400, extracted UTF-8 text cap 10 MiB; filename sanitized.
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
   - look up `(translation_key, source_hash)` for each block, record job hit/miss
     observations, and send only blocks without durable translations;
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
     Assembly derives `completed_with_errors` from missing cache rows or
     degraded blocks reported by the renderer. Billed attempt usage is
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

PDF extraction warns about visible characters unavailable in the bundled font.
Rendering drops nonprinting controls/format characters and Unicode variation
selectors, preserving newlines and tabs. A translated block with remaining
unsupported visible glyphs is excluded before redaction, retaining its original
canvas text. Other blocks render normally; final status is
`completed_with_errors` when any block degraded, even with complete cache
coverage. Diagnostics contain stages, exception class names, numeric geometry,
counts and Unicode codes, never document text or exception messages.

MVP renders translated text into the original block positions with basic
typography (font size auto-shrink bounded by a minimum readable size; blocks
that cannot fit fall back to clean regenerated pages). **Text-oriented PDFs
come out well; pixel-perfect preservation is explicitly out of scope** —
columns, complex tables, RTL, and heavy reflow are known limits, listed in
DECISIONS.md. DOCX extracts body paragraphs and paragraphs in top-level table
cells in document reading order, deduplicating merged cells. It clears all
inline content in translated paragraphs, inserts one plain run, and preserves
paragraph styles/properties and table structure. Empty paragraphs are skipped;
nested tables, headers and footers remain unchanged. Format-owned locators
distinguish body child positions from table/cell paragraph positions; historical
metadata without a container still resolves top-level paragraph indices.
This extends the original Stage 3 body-only scope. Previously extracted documents
retain their original blocks; re-uploading byte-identical files reuses persisted
extraction and does not add table blocks retroactively.
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
`fallback_count` (blocks) and `fallback_pages_count` (appended pages), and are
returned in `RenderResult` alongside degraded block IDs. Measurement and limits are
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
`estimate_usage` prices cached input separately and validates its range, while
`estimate` preserves the existing bulk pricing behavior.

**Where the agent earns its keep — triage.** Navigation uses only
`read_blocks` and `search_blocks`, starting with a bounded outline of at most
60 block heads (80 characters each), split between the beginning and end of long
documents, plus script counts. Tools retain their existing output bounds.
Safe telemetry records only tool names, numeric argument shapes and counts.

DT-100 measured five PDF/DOCX samples using `gpt-4o-mini` at 16 turns: both the
old and corrected prompts delivered an accepted plan on 1/5 (20%) documents.
Mean reported-usage cost rose from $0.00244629 to $0.00689094 with the outline.
After the owner approved independent models, `TRIAGE_MODEL=gpt-4o` delivered
accepted plans on 5/5 files (100%), averaging $0.03404600 and 3.8 requests.
`OPENAI_MODEL=gpt-4o-mini` remains the independent bulk/glossary default.
Requested Luna6 and Luna5.6 follow-up runs each accepted 5/5 plans, with mean
cache-write-adjusted estimates $0.000998178 and $0.002089002. They are explicit
TRIAGE_MODEL options; the owner subsequently selected gpt-6-luna as the default. Production token pricing excludes
the cache-write premium, separately captured only for this evaluation.
The mini run does not demonstrate convergence improvement; 4/5 required
the degraded path. The five-file gpt-4o result is a small-corpus observation,
not a population success rate. The corpus's bulk spend and triage share of total spend were
not measured. See [DT-100 execution](docs/plans/2026-10-03-triage-convergence-execution.md).

Triage ends immediately on a non-retryable `ProviderError` or a
`TriageTerminalError` after turn-budget exhaustion. The latter retains
`PROVIDER_INVALID_RESPONSE` and its catalogued `retryable=True`, preserving
bulk retry behavior. Bare injected exceptions and retryable provider failures
retain up to three attempts. Known usage from the final failed attempt still
contributes to cumulative analysis totals before degraded publication.

`TRIAGE_MAX_TURNS` defaults to 16 (1–20); it is the single navigation-call cap
with parallel tool calls disabled. `TRIAGE_TIMEOUT_SECONDS` defaults to 60
(finite, >0 and ≤300). The service guard is always five seconds longer so the
adapter can return usage on timeout. MCP retains its independent polling limit;
background analysis may continue after that wait ends.

The installed Agents SDK supplies a generated `prompt_cache_key` per run for
supported models. Cached input receives the existing discounted pricing.
`prompt_cache_retention` stays unset for these short runs; cache affinity does
not guarantee a cache hit.

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
| `POST /api/documents` | multipart upload → analyzing document + block count |
| `GET /api/documents/{id}` | document readiness: `analyzing` / `extracted` / `failed` + block count |
| `POST /api/documents/{id}/retry-triage` | explicitly recover stuck or degraded triage |
| `POST /api/jobs` | `{document_id, target_languages[], idempotency_key}` → batch of jobs |
| `GET /api/jobs?limit=10` | recent jobs, newest first (limit bounded by the service); each carries its own bulk `cost_usd` plus the document's shared `analysis_cost_usd` |
| `GET /api/jobs/{id}` | status, progress, cost, structured error |
| `POST /api/jobs/{id}/retry` | re-queue failed chunks (optional raised cost cap) |
| `GET /api/jobs/{id}/events` | SSE progress stream |
| `GET /api/jobs/{id}/download` | translated file |
| `GET /api/batches/{id}` | all jobs of a multi-language batch |
| `GET /healthz` | liveness: process is up |
| `GET /readyz` | readiness: DB reachable, storage writable, no stale inflight chunk leases |
| `GET /metrics` | Prometheus |

Job payloads carry two distinct cost figures. `cost_usd` is the job's own bulk
translation spend. `analysis_cost_usd` is the document's cumulative triage cost
from `document_analyses.cost_usd_total`, resolved for a whole page in one batched
query on job-list and batch responses and **shared by every language**
translated from that upload. It is absent
from `JobRecord`, which mirrors the `jobs` table one-to-one, and the history view
renders it once per document rather than per card. A zero means no usage was
recorded; for documents analysed before instrumentation that is not the same as a
free analysis.

Single-job GET/retry responses use the default `0.0` for this shared field;
SSE progress does not carry it. History retains the known document cost through
a retry and counts only translations visible under its current status filter.

Job summaries and SSE progress include default-zero `cache_hit_blocks` and
`cache_miss_blocks`, cumulative lookup counts in blocks, separate from chunk
progress. The client derives its percentage from hits plus misses.

Errors are structured: `{error_code, message, retryable}` — never bare
"Something went wrong".

Document responses include `analysis_cost_usd`, the cumulative document
analysis estimate across retries and re-triages (zero without an analysis row).
The batch page shows this once above its language jobs; each job cost remains
bulk translation only. Unknown provider usage is excluded.

Stage 5 implements this surface through core document/job services. Stage 6 uploads
atomically persist extracted blocks with status `analyzing`, then schedule
triage with FastAPI BackgroundTasks and a fresh SQLite connection. The agent
navigates bounded text snippets and returns `TriageAgentOutput` with a brief
evidence-based explanation and TranslationPlan. The adapter returns a
`TriageResult` with aggregate SDK input/output/cached tokens and request count.
The service prices usage and persists current-plan and cumulative totals;
per-attempt cost deltas are logged after commit without document text. Logs
are best effort: a process interruption after commit can leave a durable delta
without its corresponding log event.
Up to three bounded attempts precede a heuristic degraded fallback; terminal
provider failures stop after their first attempt. Analysis and the
`extracted` transition commit together. Jobs require completed analysis and
return `409 analysis_pending` while it is unavailable. Explicit retry recovers
process crashes without adding a schema lease. New uploads use content digest
IDs to reuse their original analysis. Stage 7 shares advisory document locks
between REST and MCP on the local data filesystem. A service commits a conditional
status claim before scheduling triage; the held lock excludes live owners and
releases on process death, allowing abandoned `analyzing` recovery without a
schema lease. Analysis uses short-lived DB connections, closed during provider
calls. Content-addressed upload locking also protects shared artifacts across
the two front doors.
Analysis becomes immutable after the first translation job; retrying a degraded
plan after that returns conflict to preserve worker/cache consistency. Atomic
enqueue rejects stale analysis terms after concurrent triage replacement.
Request connections use the shared WAL factory and close after responses, including SSE termination.
Idempotency checks run within aggregate insertion transactions, and partially
created language batches can be completed by repeating the same request.
Retries preserve cached work and billed attempts; internal retry budgets survive
process restarts in the existing diagnostic field while no public error is set.
Readiness checks DB, writable storage, and stale inflight chunk leases.
The grace is `max(120, 2 * CHUNK_LEASE_SECONDS)` seconds beyond expiry;
idle deployments cannot prove worker liveness using this heuristic.
Metrics derive job counts and known cost/error totals from persistence;
cache totals are durable sums of per-job lookup counts.
See [REST operating notes](docs/api.md).

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

MCP tools call the same core services and ports as REST directly; they do not
call FastAPI over HTTP and contain no SQL or duplicated business rules. The
container reads input files and writes downloaded results only under a
dedicated host directory mounted at `/mcp-files`; supplied paths are resolved
and checked for containment, including symlinks. `translate_file` polls the
document repository for triage completion before creating jobs. Recent-job
listing is provided by `JobService.list_recent_jobs(limit)` and the REST query
route above.

MCP job summaries include the same `cache_hit_blocks` and `cache_miss_blocks`
counts as REST, including `check_status` and recent-job results.

Stage 7 implements the four tools with explicit Pydantic success/error results.
Polling is bounded by `MCP_TRIAGE_TIMEOUT_SECONDS` (default and maximum 45s),
including repository waits and asynchronous sleeps. A pending result contains
the safe catalogued error and document ID. `check_status` accepts that document
ID in its existing string parameter and reports readiness; after extraction,
the caller repeats `translate_file` to enqueue. MCP submissions use stable
content/language idempotency keys and return immediately after enqueue.
Downloads are atomically copied into validated shared output directories.
`download_result` distinguishes a host-side permission fault
(`shared_dir_unavailable`, not retryable) from other filesystem errors
(`internal_error`, retryable), because no client action resolves directory
ownership; startup reports an unusable share once as
`mcp_shared_dir_not_writable` without stopping the read-only tools.
FastMCP owns startup/cleanup, initializes WAL before tools are accepted, and
releases claimed triage work during shutdown. `python -m app.mcp_server` serves
`0.0.0.0:8001/mcp`; Compose shares one `/data` volume across all processes.
Host bind source
`MCP_HOST_SHARED_DIR` is separate from the in-container `MCP_SHARED_DIR`.

---

## 10. Frontend

React + Vite + TS + Tailwind, branded for **Stark** using the
starkfuture.com identity: primary `#FF1717`, background `#000000`, dark
surfaces `#1E1E1E` / `#242424`. The wordmark is committed as a local SVG and
colours are hardcoded theme tokens; remote asset hotlinking is rejected
(DECISIONS.md §9). Three views:

1. **Upload** — drag-n-drop, client-side pre-validation, server errors
   rendered specifically ("This PDF has no text layer (scanned document)",
   "422 pages exceeds the 400-page limit"). After upload the UI polls
   `GET /api/documents/{id}` and issues one `POST /api/jobs` once the document
   is `extracted`; it never polls `POST /api/jobs` for readiness.
2. **Job view** — one card per target language, chunk-level progress bar via
   SSE, live cost counter and a cached-block percentage (hits / total lookups);
   `completed_with_errors` renders distinctly
   ("3 of 412 blocks could not be translated — kept in English. Retry.");
   failure states carry a concrete action.
3. **History** — past jobs, status filter, re-download. The document analysis
   cost is shown once per document and labelled with the number of translations
   sharing it, so three languages do not read as three separate charges; a zero
   renders nothing rather than a misleading `$0.0000`.

---

## 11. Failure handling matrix

| Failure | Behavior |
|---|---|
| Corrupt PDF | Magic-byte + open failure → `corrupt_file`, HTTP 422, no job created |
| Scanned PDF | No text layer → `scanned_pdf`, explains OCR is unsupported |
| >400 pages / >50 MiB | Rejected at upload: `page_limit` / `size_limit` |
| Unsupported type | `unsupported_format` with the list of supported types |
| Provider 429/5xx/timeout | Per-chunk retry, exp backoff + jitter, ≤4 attempts; exhausted → chunk fails, job continues → `completed_with_errors` |
| Provider 400 / context length | Fatal for the chunk immediately (not retryable), surfaced distinctly |
| Ambiguous timeout (provider may have processed) | Retried → duplicate billing is possible. Reported usage is recorded when available; otherwise the charge is unknown and excluded from attempt/job cost totals (or no row exists if the worker stops before recording it). |
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
  translation is ever re-requested from the provider** (the compose script
  compares persisted attempts for already-done chunks); newly executed pending
  and interrupted chunks may add attempt rows. Known attempt spend is durable;
  usage lost before checkpointing is unknown. Plus
  `scripts/chaos-restart.sh` running the same scenario against real compose —
  the reviewers' exact test.
- **Regression pins:** corrupt file, oversized file, duplicate idempotency
  key, unsupported type, retry-after-partial-failure — each a test that fails
  if behavior regresses.

---

## 13. Observability

- **Logs:** structlog JSON; `job_id`, `chunk_id`, `request_id` on every line;
  worker logs claim/heartbeat/retry/commit events.
- **Metrics (`/metrics`):** `jobs_by_status`, bulk `llm_cost_usd_total`,
  `llm_errors_total`, document `llm_triage_cost_usd_total`, and
  `llm_triage_tokens_total{direction="input|output"}`, `cache_hits_total` and
  `cache_misses_total`, aggregated from durable job columns. Cost metrics
  represent provider-reported usage estimates, not total billing; bulk and
  document-level triage series remain separate.
- **Health:** `/healthz` (liveness) and `/readyz` (DB, storage,
  stale inflight leases) — not conflated.
- **Runbook:** README section "3 a.m." — what to look at first, mapped to the
  metrics above.

Stage 9 delivery exports durable job counts and known cost/error totals; these
cost totals are estimates from provider-reported usage, not complete billing.
`llm_cost_usd_total` remains bulk job expense; `llm_triage_cost_usd_total` and
`llm_triage_tokens_total{direction="input|output"}` read cumulative analysis
columns independently of jobs. A fresh registry per scrape prevents recounting,
and durable totals survive restart.
`cache_hits_total` and `cache_misses_total` sum job lookup counts and survive
restart without being recounted by scrapes. Window hit rate is
`Δhits / (Δhits + Δmisses)` when the denominator is positive. Re-delivery
may count another observation; a crash before persistence can omit one.
Latency histograms, error-code labels, retry-share metrics, and direct worker
heartbeat age are not exported; the measurement command derives available
latencies and known retry spend from `chunk_attempts`, but cannot include usage
that was never reported. Compose health checks web HTTP
liveness, worker process liveness, and the MCP transport without adding public
health endpoints. Readiness cannot detect an absent idle worker.

---

## 14. Cost & performance measurement plan

- Pricing table per model; the worker records each attempt outcome when it can
  persist it. `job.cost_usd` sums application estimates from provider-reported
  usage, including known usage on retries; it is not a provider invoice total.
  Document expense is the sum of its jobs plus one cumulative triage total
  from `document_analyses`; usage not returned after an ambiguous failure is
  unknown and excluded. Historical triage before instrumentation cannot be
  reconstructed and existing rows initialize to zero.
- DECISIONS.md reports, on a fixed sample document, the **persisted bulk usage
  estimate per document** and what it includes (including recorded retry
  share), observed chunk/job durations, and the chrF proxy score. It reports
  population p95 values only when the sample supports them; otherwise it gives
  observed durations and marks the p95 comparison unmeasured. It distinguishes
  estimates from total provider billing and states other measurement gaps or
  unavailable usage explicitly.

---

## 15. Conscious cuts (seed for DECISIONS.md)

| Cut | Why | Cost of cutting |
|---|---|---|
| OCR for scanned PDFs | Tesseract/vision pipeline is a project of its own | Scanned docs rejected with a clear error |
| Pixel-perfect PDF layout | Reflow/overflow handling is unbounded | Text-oriented PDFs only, stated in §6.6 |
| Horizontal worker scaling | Single worker with bounded async concurrency is the chosen assessment design; no multi-worker scaling comparison is claimed | Scaling out would require stronger queue/claiming semantics (real broker); stated, not built |
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

1. PDF bbox insertion: measure overflow/fallback rate on the sample doc; if
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
| At least two input formats | PDF + DOCX + Markdown via the Opaque Metadata pattern | §2, §4, §6.6 |
| Frontend free choice | React + Vite + TypeScript SPA served by FastAPI | §7, §10 |
| `docker compose up --build` works from a fresh clone | Single Docker image, three processes, shared `/data` volume | §3, §16 |
| `README.md`: architecture, decisions, testing guide | Required deliverable; includes quickstart, testing guide, 3 a.m. runbook | §16 |
| `PROMPTS.md`: AI-usage log incl. rejected output | Required deliverable; maintained as work proceeds | §16 |
| `DECISIONS.md`: cuts, trade-offs, measured cost/p95, 3 more weeks | Required deliverable; reports supported measurements and states gaps, including where persisted usage does not establish total provider billing | §16 |

### 18.2 Evaluation criteria from the brief

| Criterion | How the design addresses it | Section |
|---|---|---|
| **Agency / product judgment** | Explicit target user and three acceptance criteria written by the team | §1.1, §1.2 |
| **Engineering fundamentals — data model & API** | Document IR, job/chunk/attempt model, REST surface | §4, §5, §8 |
| **Engineering fundamentals — layering** | `core/` is independent; `api/` and `mcp_server/` are thin doors | §3, §4 |
| **Engineering fundamentals — concurrency** | Bounded async parallelism (semaphore 8); short DB transactions | §5.2, §6 |
| **Engineering fundamentals — idempotency & retries** | `idempotency_key`; exactly-once cache; at-least-once invocations, with only provider-reported usage represented in attempt cost totals | §5.1, §11 |
| **Engineering fundamentals — failure handling** | Failure matrix covering corrupt, scanned, oversized, provider errors, crashes | §11 |
| **Engineering fundamentals — tests** | Unit, contract, integration, chaos, regression pins | §12 |
| **Engineering fundamentals — observability** | Structured logs, Prometheus `/metrics`, `/healthz`, `/readyz`, 3 a.m. runbook | §13 |
| **Product judgment — quality metric** | Tested core chrF and literal preservation, published-reference two-model baseline, and explicitly labelled back-translation proxy | §1.3, §14 |
| **AI leverage** | `PROMPTS.md` logs delegation, rejection, and correction | §16 |
| **UX — Stark branding** | React UI using the verified starkfuture.com palette and a local wordmark SVG | §10 |
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
- A large representative reference corpus, tables and long-document fidelity
  remain deferred. The committed 20-pair FLORES-200 sample provides a narrow
  reference baseline; without a supplied reference, the CLI reports
  back-translation chrF as a proxy. Live figures require real credentials.

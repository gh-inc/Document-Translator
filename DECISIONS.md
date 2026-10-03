# DECISIONS.md

**Status:** Living document. Decision records are written as decisions are
made. Measurements and their known limits are recorded in the relevant
sections; gaps are labeled explicitly.

This file records what we chose **not** to build and why, the trade-offs we
took knowingly, and what we would build with three more weeks. The
assessment's own rule applies: stated gaps read as judgment; silent gaps
read as oversights.

---

## 1. Decision record: single-runtime architecture (Python 3.12) vs. polyglot (Node.js + Python)

**Context.** The system combines high-concurrency I/O (async web server, SSE
streaming, parallel LLM API calls) with low-level document processing (PDF
coordinate extraction, document-tree manipulation, the `openai-agents` SDK).
Its deployment model is three processes — web, worker, MCP — sharing one
SQLite database and one filesystem volume, and it must survive forced
container restarts (`kill -9`) without state corruption.

**Evaluated alternatives.**

1. **Polyglot — Node.js for API/MCP + Python worker.**
   - *Pros:* Node's event loop is a natural fit for the API/SSE tier.
   - *Cons:* double containerization and dependency overhead; two data-model
     definitions to keep synchronized (TypeScript interfaces vs. Pydantic
     models); cross-runtime SQLite access — differing driver defaults for
     transactions and busy timeouts — invites lock contention
     (`database is locked`) precisely under the concurrent web/worker load
     this system is designed for; IPC or shared-volume coordination between
     runtimes adds a failure surface with no compensating capability gain.
2. **Pure TypeScript / Node.js.**
   - *Pros:* one language across the entire stack.
   - *Cons:* the Node PDF ecosystem has no mature equivalent of PyMuPDF's
     coordinate-precise extraction and in-place rendering; TypeScript
     support for `openai-agents` is younger and less battle-tested than the
     Python SDK — the two most failure-prone parts of this system would sit
     on its two weakest libraries.
3. **Pure Python 3.12 (FastAPI + worker + FastMCP) — CHOSEN.**
   - *Pros:* one runtime, one dependency set, one Docker image, one test
     runner; mature document libraries (PyMuPDF, python-docx) and the
     reference implementation of the agents SDK; uniform SQLite access
     semantics (WAL mode) from all three processes, with no cross-runtime
     driver or locking mismatches; `asyncio` + FastAPI + uvicorn covers the
     I/O-bound concurrency profile (SSE, parallel LLM calls) without
     introducing a second runtime.

**Rationale.** In a system that must survive forced container restarts
without state corruption, minimizing inter-process and inter-runtime
coordination is the primary reliability lever: every runtime boundary is a
place where state can diverge and where a lock or a message can be lost. A
single runtime provides one point of control over database state and
eliminates an entire bug category — cross-runtime model drift, where a
TypeScript interface and a Pydantic model silently disagree about a field.
The performance cost is negligible for this workload: the system is
I/O-bound (end-to-end latency is dominated by the LLM provider), and
Python's async stack handles I/O-bound concurrency without strain.
Development velocity is a second-order but real benefit: one language, one
type system, and one test stack across web, worker, and MCP.

---

## 2. Decision record: AI provider & cost strategy (model selection)

**Context.** The translation engine must balance quality (preservation of
whole-document context and glossary terminology) with predictable unit
economics. The system will process business documents, not literary prose;
absolute fluency matters less than terminological consistency and cost
transparency.

**Evaluated alternatives.**

1. **Dedicated translation APIs (DeepL, Google NMT).**
   - *Pros:* Predictable per-character pricing.
   - *Cons:* Roughly $20–25 per 1 million characters for production tiers;
     limited ability to inject whole-document context or a dynamic glossary
     per job. The per-document TranslationPlan and glossary are first-class
     features of our design, so a provider that cannot consume them is a
     poor fit.
2. **Heavy LLMs (GPT-4o, Claude 3.5 Sonnet / newer equivalents).**
   - *Pros:* Best available quality for complex terminology and nuanced
     register.
   - *Cons:* An order of magnitude more expensive than smaller models
     (roughly $3–9 per 1 million tokens). For the assessment's volume of
     200–400-page documents, this would dominate unit cost without a
     measured quality benefit for standard business text.
3. **Lightweight LLMs (`gpt-4o-mini`) — CHOSEN.**
   - *Pros:* Roughly $0.15–0.60 per 1 million tokens, i.e. tens of times
     cheaper per character than dedicated NMT APIs and roughly an order of
     magnitude cheaper than flagship LLMs. It still accepts the full system
     prompt with the persisted TranslationPlan, glossary, and neighboring
     source-block context — something classical NMT cannot do.
   - *Cons:* Potential minor loss of stylistic polish, which is not critical
     for business documentation.

**Rationale.** `gpt-4o-mini` is the default engine. For standard business
text, the cost lands in the order of $0.20 per 1 million characters, which
lets us keep per-document cost low while still feeding document context
(the persisted TranslationPlan + glossary) and source-side block context
into every prompt. The model is configurable via `OPENAI_MODEL`, so a
heavier model can be selected where measured quality justifies the
additional spend.

**Measured cost.** Stage 9 measured bulk translation of `samples/sample_en.pdf`
(2 pages, 12 blocks, 1 chunk) to German at **$0.00041715** with `gpt-4o-mini`.
The reverse quality-check job adds $0.00040785. These are known recorded bulk
attempt costs using the adapter's pricing snapshot; triage spend and usage lost
before checkpointing were excluded in that historical run. New analyses record
triage separately with cached-aware pricing; document expense is bulk job costs
plus one `document_analyses.cost_usd_total`, independent of language count.
See “Stage 9 live measurements” below.

---

## 3. Decision record: document format handling

**Context.** The pipeline must accept at least two input formats (PDF, DOCX)
and return "the same file, translated". The architectural question: how much
of each format's layout semantics does the translation core need to
understand?

### Rejected option 1 — Universal Document IR with full layout normalization

A single rich intermediate representation (typed blocks, style trees, table
grids, column geometry) that every extractor normalizes into and every
renderer reconstructs from.

**Why rejected:**

- **Over-engineering for a 3-day MVP.** The IR, not the translation, becomes
  the project. Every fidelity bug moves into a model-mapping layer — the
  layer we control least and understand worst.
- **Style mapping breaks exactly where it hurts.** Translation changes text
  length (EN→DE ≈ +30%), so normalized style/geometry mappings need
  per-format reflow logic anyway. The normalization buys abstraction, not
  simplicity.
- **Fidelity is bounded by the weakest normalized concept.** Anything the IR
  cannot express is lost even for formats that natively support it.

### Rejected option 2 — Markdown bridge (PDF → MD → translate → MD → PDF)

Convert every input to Markdown, translate the Markdown, convert back.

**Why rejected:** fatal loss of the original document's layout and
formatting. "The same file, translated" is the product requirement; a
reflowed Markdown rendering of a contract or a report is not the same file.
Tables, multi-column layouts, headers/footers, embedded images, and fonts do
not survive the round trip.

### Accepted — Opaque Metadata pattern + shared disk

- The core pipeline (chunker, worker, LLM provider, cache, queue) handles
  **only `seq` + `source_text`**.
- Each format's extractor records whatever its renderer will need — PDF:
  page/bbox/font size; DOCX: paragraph/run indices — into an opaque JSON
  `format_metadata` field on the Block. The core persists it and hands it
  back without inspection; the invariant is pinned by a test.
- The original upload is kept on the shared volume
  (`/data/uploads/{document_id}`), and the renderer **re-opens it as the
  canvas**, placing translations via `format_metadata`. Fidelity of
  everything the extractor never touched (images, headers, styles) is
  preserved by construction, not by reconstruction.

**Consequences.** Format knowledge is fully quarantined in adapter modules;
the core stays format-agnostic and testable with synthetic blocks. Adding a
format also changes allow-lists, detection, three composition roots and browser
upload/download behavior (see ARCHITECTURE.md, “Layering & the Document IR”).
The costs, taken knowingly:
`format_metadata` is untyped across the core boundary (mitigated by
per-adapter schemas and per-format contract tests), and renderers depend on
the original file being available — acceptable on a single shared volume,
revisited under horizontal scaling (§4).

### Markdown as the third format (2026-10-03)

Markdown headings, paragraphs, bullet/numbered list items, quotes and table
cells are translated. Supported table rows require both outer pipes; GFM tables
without outer pipes are outside the supported grammar and are treated as prose.
Fenced code and supported table separator rows pass through
unchanged. Markdown tables are translated; DOCX table cells remain outside
extraction. Inline emphasis travels as text without a styling guarantee;
reference links, images, HTML blocks and frontmatter parsing remain out of scope.
The inherited triage policy rejects uploads with zero extracted blocks as
`corrupt_file`; code-only or blank Markdown therefore cannot enqueue. Fenced
code passes through when the document also has extracted text or table cells.

Table geometry is reconstructed from adapter-owned metadata. Empty cells keep
blocks and their positions but never enter chunks, bulk provider calls or the cache.
An injected service callback asks the adapter to classify persisted blocks,
so enqueue works in another request/process and after restart. The core never
reads the metadata. RenderResult reports structural blocks as satisfied, so a
complete merged-cell table finishes `done`.

Empty-cell bypass preserves bulk translation token usage and cost for the
same translated content. Triage still receives the full document IR for
navigation; live triage expense may vary and is not claimed invariant.

The registry validates the Markdown UTF-8 header and rejects NUL bytes;
PDF/DOCX signature checks are unchanged. A binary file renamed `.md` whose
header is valid UTF-8 without NUL can pass detection; extraction validates the
whole text. Literal model-generated pipes and cell newlines are sanitized to
preserve table geometry. This demonstrates the Opaque Metadata boundary with
a third native format; it does not introduce a PDF/DOCX-to-Markdown bridge.

---

## 4. Decision record: triage agent execution model

**Context.** Triage uses the OpenAI Agents SDK with tool calling. It must not
block the HTTP upload response, must survive ordinary failures, and must not
become a single point of failure.

**Evaluated alternatives.**

1. **Synchronous triage in the upload request.**
   - *Pros:* Simple, no background state.
   - *Cons:* Agent latency (seconds to tens of seconds) blocks the HTTP response
     and the client; violates the requirement that a real document takes minutes
     and the API must not wait.
2. **Persistent triage queue processed by the worker.**
   - *Pros:* Survives `kill -9`; reuses the same lease/claim machinery as
     translation chunks.
   - *Cons:* Adds a new queue entity to Stage 4, increasing complexity and
     coupling for the MVP.
3. **FastAPI BackgroundTasks + retry endpoint — CHOSEN.**
   - *Pros:* Zero infrastructure overhead; trivially isolates agent latency from
     HTTP; keeps the worker focused on translation.
   - *Cons:* Background tasks live in process memory; a `kill -9` during triage
     leaves the document stuck in `analyzing`. Mitigated by the
     `POST /api/documents/{id}/retry-triage` endpoint.

**Rationale.** For a 3-day MVP, `BackgroundTasks` are the pragmatic choice. The
stuck-state risk is explicit and recoverable via the retry endpoint. A durable
worker-owned triage queue is reserved for the "three more weeks" list.

## 5. Other conscious cuts

Seeded from ARCHITECTURE.md §15; each gets a closing paragraph with measured
impact at the end of implementation:

- OCR for scanned PDFs (rejected with a clear `scanned_pdf` error instead)
- Pixel-perfect PDF layout (text-oriented fidelity only, ARCHITECTURE.md §6.6)
- Horizontal worker scaling (single worker + bounded async concurrency; no
  multi-worker scaling comparison was measured, and none is claimed)
- Auth / multi-tenancy
- Glossary editing UI (triage glossary is automatic)
- Sequential polish pass with translated context
- Side-by-side preview / in-place editing

---

## 6. Measured numbers

Measurements are recorded in two places. The fixed-sample PDF renderer and
layout results are in [Stage 3 format measurements](#stage-3-format-measurements).
The live provider run reports cost, observed chunk and job durations, the
back-translation chrF proxy, preservation checks, and the limits of those
measurements in [Stage 9 live measurements](#stage-9-live-measurements).

Several requested comparisons remain explicitly unmeasured: a population p95
job latency, before/after chunk-parallelism latency, date/currency/placeholder
preservation, supplied-reference chrF, and the historical run’s full provider
billing, whose triage and ambiguous uncheckpointed usage cannot be reconstructed.
The Stage 9 table gives the reason for each gap. Its single forward chunk latency is one observation, not a
population estimate.

### Stage 3 format measurements

The fixed `samples/sample_en.pdf` contains two pages and twelve extracted text
blocks: nine headings/paragraphs and three table rows. Reproduce the layout
measurement with:

```bash
uv run python scripts/generate_sample_docs.py
uv run pytest tests/adapters/formats/test_pdf_renderer.py -s
```

| Input to renderer | Fallback blocks | Appended pages | Final pages |
|---|---:|---:|---:|
| FakeProvider `[de]` prefix | 3 / 12 (25%) | 3 | 5 |
| Same output with at least 30% synthetic text expansion | 3 / 12 (25%) | 3 | 5 |

Every translated block is present in the parsed output. All nine
heading/paragraph blocks fit inside their original bboxes after font shrinking
to a minimum of 6 pt. The three table rows are represented by the extractor as
multiline blocks; they move to appended pages. Their original vector grid
survives, with empty rows where the source text was removed. The rendered sample
was also visually inspected. This is an explicit table-layout limitation, not
pixel-perfect table preservation.

**Decision:** retain bbox insertion as the default for text-oriented documents:
the sample's ordinary text fits even with the synthetic expansion, and overflow
does not lose content. Table-heavy documents need adapter-specific cell layout
or a clean-regeneration strategy in later work. Changing the default to full
regeneration now would discard the original canvas without improving the
sample's ordinary paragraphs. This measurement does not assess German quality:
FakeProvider only prefixes text. The live run's back-translation chrF proxy,
cost, and observed durations are recorded in [Stage 9 live measurements](#stage-9-live-measurements);
the proxy does not establish actual translation quality.

The renderer embeds PyMuPDF's bundled Droid Sans Fallback font buffer under a
private name so insertion uses the same glyph widths as fitting. A CJK alias
alone produced clipped Latin lines in review and was rejected. Glyph coverage
is checked before changing the canvas. Nonprinting controls/format characters
and Unicode variation selectors are removed while newlines and tabs survive.
If a visible glyph remains unsupported, that entire block keeps its original
canvas text and the job finishes `completed_with_errors`; supported blocks
still render. Upload warns with Unicode codes and proceeds. This avoids losing
a large document because of one icon without introducing glyph substitution
tables. These transient warnings require no database change. Complex shaping,
RTL, and pixel-perfect typography remain
outside the supported PDF scope. Per-render structured logs report both
fallback blocks and appended pages; long-block tests prove full pagination,
including Cyrillic and CJK, without truncation.

DOCX samples use top-level paragraph blocks, one plain replacement run, and
preserved paragraph styles/properties. Inline formatting/hyperlinks are removed
only in translated paragraphs. Table cells, headers, and footers stay unchanged;
their translation is outside the approved Stage 3 scope. Both generated sample
files have fixed metadata and reproduce byte-for-byte in tests.

---

## 7. Future work — what three more weeks would buy

1. **Durable triage queue.** Move triage from FastAPI BackgroundTasks into the worker's claim loop with leases and retries, eliminating the stuck-`analyzing` risk and the need for a manual retry endpoint.
2. **Object storage (S3/MinIO) instead of the shared volume.** The shared
   disk is the one thing pinning web, worker, and renderer to the same
   filesystem. Moving uploads and outputs to object storage (presigned
   up/downloads; the renderer fetches its canvas object) is the
   prerequisite for horizontal worker scaling across physical nodes — the
   stated ceiling of the current single-worker, single-volume design.
3. **Multi-worker queue semantics** (a real broker with visibility timeouts),
   unblocked by (2).
4. **OCR fallback** for scanned PDFs (vision model or Tesseract) behind the
   same Block abstraction — `scanned_pdf` stops being a rejection.
5. **Glossary override UI** on top of the persisted TranslationPlan.
6. **Sequential polish pass** using translated context for long-range style
   coherence (the rejected §6 mechanism, reintroduced as an optional
   post-pass where serialization is acceptable).

7. **Local model provider (Ollama / GGUF).** Deferred beyond the three-day
   assessment. A local quantized model could exercise real prompt/schema
   adherence between FakeProvider and paid OpenAI runs, catching integration
   failures without provider charges. It would require its own LLMProvider
   adapter, model/runtime setup and validation; it is not implemented here.

---

## 8. Decision record: MCP adapter and filesystem boundary

**Context.** The MCP server and REST API are two front doors to one core. MCP
must reuse the established service behavior, expose the approved editor
workflow tools, and handle files even though it runs in a separate container.

**Evaluated alternatives.**

1. **MCP calls the local FastAPI server over HTTP.**
   - *Pros:* Reuses REST endpoints and avoids constructing services in MCP.
   - *Cons:* Adds a loopback/network dependency between components in the same
     application, duplicates HTTP error and retry handling, and does not solve
     the host-file-path visibility problem for a containerized MCP server.
2. **MCP composes the same core services and ports directly — CHOSEN.**
   - *Pros:* Both front doors use the same application logic without a network
     hop; MCP remains thin and does not contain business rules or SQL.
   - *Cons:* MCP needs its own dependency composition and active-connection
     lifecycle, which must remain consistent with the API composition.

**File access boundary.** An HTTP MCP container cannot read arbitrary paths on
the client host. MCP input and output therefore use a dedicated host directory
mounted into the container. Tools resolve every path and verify it remains
within that mount, including symlink resolution; mounting the whole host
filesystem is rejected.

**Recent-job lookup.** The approved MCP `list_recent_jobs` tool is backed by
`JobService.list_recent_jobs(limit)`. REST exposes the same service operation at
`GET /api/jobs?limit=10`; query parameter routing avoids creating a separate
`/recent` route and keeps business logic in the core service.

**Triage readiness.** `translate_file` polls document status through
`DocumentRepository` until analysis completes, then calls `JobService` to
create jobs. It does not poll REST endpoints. Each poll uses a short-lived DB
connection, and no transaction is held during triage network calls or polling
sleeps. A bounded deadline returns a retryable result containing the document
ID so a later invocation can resume.

---

## 9. Decision record: frontend branding source and SPA fallback

**Context.** The brief requires the web interface to be branded for Stark and
served by the application. Two distinct companies use the Stark name, and the
architecture previously referenced `getstark.co`, an accessibility-software
brand whose palette is teal, purple, bone, and yellow.

**Branding source.**

1. **`getstark.co` palette — rejected.** Wrong company. Its brand colors do not
   match the red/black identity the user approved, and reusing another
   company's marks and palette would misrepresent the brand.
2. **`starkfuture.com` identity — CHOSEN.** Verified from the site's production
   CSS and assets:
   - Stark red: `#FF1717` (`.color-stark-red`).
   - Black: `#000000` (`--black-100`, header background).
   - Supporting dark neutrals: `#242424` and `#1E1E1E`.
   The site exposes no official semantic "secondary" token; `#242424` is used as
   the application's secondary surface alias, and that distinction is stated
   rather than implied.

**Asset policy.** The wordmark SVG is downloaded into the repository and served
locally. Runtime hotlinking of remote images, fonts, or stylesheet assets is
rejected: it makes the UI depend on third-party availability and would break
offline operation.

**SPA fallback safety.** The approved execution refinement uses a custom 404
exception handler after API router registration. `/api` and `/api/...` always
keep the structured `{error_code, message, retryable}` JSON 404 envelope.
GET/HEAD browser navigation falls back to the local `index.html`; missing
static resources and non-navigation methods remain 404. Existing files are
served only from the contained frontend build. This replaces the original
catch-all proposal and keeps API typos from becoming HTML responses.

---

## Stage 9 live measurements

Measured on **2026-10-03 Europe/Kyiv** (start **2026-10-02 21:49:42 UTC**)
with `LLM_PROVIDER=openai`, model `gpt-4o-mini`, and the committed sample
`samples/sample_en.pdf`: 2 pages, 12 source blocks, one chunk per direction.
Sample SHA-256: `23b434b65b2a43240da6540c811273575ac8c08618eeb0ba036873621eb0aa8d`.
The machine-readable [run report](docs/measurements/2026-10-03-sample-en.json)
contains per-job persisted usage, times, IDs, and metric conventions.

Reproduce with:

```bash
uv run python -m scripts.measure_quality samples/sample_en.pdf --env-file .env --timeout 240
```

| Figure | Observed result | Scope |
| --- | --- | --- |
| Forward cost per document, EN→DE | $0.00041715 | 801 input + 495 output tokens, one successful bulk attempt |
| Back-translation cost, DE→EN | $0.00040785 | 823 input + 474 output tokens, one successful bulk attempt |
| Combined bulk spend | $0.000825 | Two jobs; output tokens account for $0.0005814 (70.47%) at the adapter pricing snapshot |
| Recorded bulk retry share | 0% | No bulk retry attempts; triage had retries whose spend is unrecorded |
| Forward chunk-attempt latency | 6645 ms | One observation; the reported nearest-rank p95 equals this single sample |
| Reverse chunk-attempt latency | 5153 ms | One observation |
| Forward / reverse persisted job latency | 7108 / 6211 ms | Enqueue-to-terminal duration; excludes preceding extraction/triage |
| Population p95 job latency | not measured | Two different-direction jobs cannot establish a useful p95 |
| Before/after chunk parallelism | not measured | One chunk per job provides no parallelism comparison |
| Back-translation chrF | 85.2706 / 100 | Case-sensitive chrF β=2, orders 1–6, effective-order means, whitespace excluded |
| Number/Placeholder Preservation | 100% (5/5) | Forward rendered text, strict literal multiset comparison; all five tokens are numbers |
| Dates / currency / placeholders | not measured | The fixed sample contains none of these tokens; the command reports null for empty categories |
| Supplied-reference chrF | 71.3698 (gpt-4o-mini), 70.8356 (gpt-4o) / 100 | DT-92 adapted 20-pair FLORES-200 reference run below; historical Stage 9 had no supplied reference |
| Triage-inclusive recorded expense per document | Sum of job costs + one document triage cumulative cost | Available for newly instrumented runs; historical Stage 9 triage was not recorded and cannot be reconstructed |
| Exact provider invoice total | unknown | Ambiguous or uncheckpointed usage may be unavailable; application prices are estimates |

Forward triage completed successfully on its third attempt; reverse triage
exhausted three attempts and used the documented degraded fallback. The forward
PDF renderer reported three overflow fallback blocks. The chrF score therefore
measures this rendered pipeline run with its actual triage/fidelity limits; it
is a coarse information-preservation proxy, not a translation-quality verdict.
The output-token cost dominates only the measured bulk spend. No fake-provider
output is used for any figure in this section. Provider prices may differ from
the adapter's recorded pricing snapshot.

### Triage cost observability (2026-10-03)

`document_analyses` now stores current-plan tokens/cost and separate cumulative
`_total` columns. Every reported attempt, including failed attempts and retries,
adds to the cumulative figures. Current-plan values describe the successful
plan now in force; degraded fallback has no successful provider-plan usage.
Replacing a degraded analysis preserves cumulative expense in the same service
transaction. Successful/frozen analyses are reused without a new provider call.
One triage belongs to one document, even when it creates three language jobs.

The Agents SDK aggregates usage on `context_wrapper.usage`, including the
context accumulated by tool calls. The adapter retains that wrapper so reported
partial usage can reach failure accounting. Missing usage defaults to zero;
this means unknown, never proof of a free provider request. Cached input is
priced at the supported models’ explicit cached rates, currently 50% of their
input snapshot rates. No approximate cache discount is assumed. Unsupported
configured models retain token counts, log `triage_cost_estimation_failed`, and
record zero estimated cost until their pricing snapshot is added; this is an
unpriced expense, not evidence of a free request.

`/metrics` reads the cumulative columns in a fresh registry per scrape;
`llm_triage_cost_usd_total` and `llm_triage_tokens_total` remain separate from
bulk job cost. After publication commits, `triage_cost_recorded` logs attempt
deltas with identifiers, model, token/request counts and estimated cost, never
source text. These audit logs are best effort: a crash between commit and log
emission can leave durable cumulative expense without its per-run log event.

Offline regression fixtures (synthetic usage, not a live provider measurement):

| Figure | Verified estimate | Scope |
| --- | --- | --- |
| Triage-inclusive document expense before bulk execution | $0.0000024 | One controlled triage: 10 input, 4 cached input, 2 output; three queued language jobs each have zero bulk spend |
| Cumulative document triage after degraded retry | $0.0000282 | Five reported attempts, 137 input and 25 output; the current successful plan alone costs $0.000021 |

These fixtures verify the accounting formula and retry behavior; they do not
replace historical live measurements.

The historical Stage 9 live figures above remain unchanged: no triage usage
was captured then. Existing rows initialize all six accounting fields to zero.
No retroactive reconstruction and no new live measurement were performed for
this change. Newly recorded document expense is computable from the durable
rows; it still excludes usage unavailable after an ambiguous failure or lost
before publication, and is not an exact provider invoice.

---

## 10. Decision record: worker liveness in readiness

**Context.** `/readyz` must answer "is this deployment able to serve work?"
Stage 5 deliberately checked only database and storage reachability and deferred
worker freshness. This stage closes that gap.

**Evaluated alternatives.**

1. **Dedicated `worker_heartbeats` table or marker file.**
   - *Pros:* Explicit, direct signal per worker instance.
   - *Cons:* New schema and a new write path on every heartbeat; would require
     the worker to write outside its existing lease protocol. Useful only when
     many workers must be balanced or killed individually.
2. **Stale-lease heuristic over existing chunk state — CHOSEN.**
   - *Pros:* Uses state the worker already maintains through its lease
     protocol. No schema change, no new write path, no extra configuration.
   - *Cons:* Indirect signal rather than a direct heartbeat.

**Mechanism.** Readiness runs a bounded query over `chunks` for rows still
`inflight` whose `lease_expires_at` is older than a grace period exceeding
`CHUNK_LEASE_SECONDS` (120 s against a 60 s default). A non-zero result means no
worker has renewed those leases and the deployment reports 503 with the
catalogued `not_ready` envelope.

**Stated limitations.** An idle deployment with no in-flight chunks reports
ready, so absence of evidence is not proof of a live worker. A chunk that is
genuinely slow or whose process is paused past its lease is reported as not ready
until it recovers. Both are properties of an indirect signal and are documented
rather than hidden.

**Chaos verification without new instrumentation.** Restart-under-chaos is
verified from durable state using the `sqlite3` CLI against `chunk_attempts`,
`block_translations`, and `chunks`, rather than by adding invocation logging to
`FakeProvider`. Because a chunk killed while `inflight` has no committed
translation and is re-executed by design, attempt rows may increase for exactly
that chunk. The guaranteed invariant is that committed translations are never
re-requested and never duplicated.

## 11. Decision record: content-addressed translation cache

**Context.** A two-word edit changed the document digest and every block ID,
so unchanged paragraphs missed the old document-scoped cache. The provider
renders the entire analysis plan, but the old key did not include it.

**Decision.** Key durable translations by exact source SHA-256 plus target
language, model, prompt version, canonical glossary and the entire rendered
plan. Keep block IDs document-scoped. Exclude neighbouring source context,
as approved by the owner, to retain reuse when adjacent paragraphs change.
Drop legacy cache rows rather than backfill keys that cannot be produced again.

**Why and consequence.** Many business paragraphs are self-contained, making
exact reuse useful; hashing neighbours would invalidate unchanged paragraphs
on common edits. A context-dependent paragraph can reuse a translation whose
meaning differs from a fresh translation in its new surroundings. This is an
accepted quality trade-off, reversible by adding neighbour hashes. Hit/miss
metrics measure reuse, not semantic correctness or this quality risk.

**Measured presentation.** Default-zero job counts feed REST, SSE, MCP and
Prometheus. The UI derives its percentage over hit plus miss observations and
labels the unit as blocks, not execution chunks. Lookups are counted durably
before provider work, including failed executions; retry/re-delivery may add
another observation. A crash before this write can undercount. Simultaneous
misses can invoke the provider twice while committing one cache row.

**Rejected.** Content-derived block IDs (global PK/provider ID conflicts),
embedding/fuzzy matching (not exact semantic reuse), neighbour-hashed keys
(reduced reuse), and a server-computed ratio (duplicate rounding definition).

**Verification and measurements.** Offline FakeProvider regression evidence
will be recorded in the execution record. No live repeat-cost comparison is
claimed; cache hits skip bulk provider calls but do not prove total billing
savings or remove independent triage/glossary work.

## Reference-based model benchmark (2026-10-03)

Measured EN→DE with the same 20-pair FLORES-200 `devtest` subset for
`gpt-4o-mini` and `gpt-4o`, via DOCX extraction/translation/rendering. German
prose was copied from the official corpus. Sentence IDs, hashes, CC BY-SA 4.0
attribution and identical synthetic literal suffixes are documented in
[samples/golden_dataset.md](samples/golden_dataset.md).
The tested core metrics retain the existing chrF conventions. These scores
compare document text streams, not the full FLORES corpus or an official
sentence-segmented leaderboard implementation.

Final matrix (UTC starts 19:11:59 and 19:12:38):

Quality mode: reference

| Model | Cost / 1M tokens in+out (USD) | Known run cost (USD) | chrF | Preservation % | Recorded bulk requests | Tokens in / out |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-4o-mini | $0.1300 | $0.004092 | 71.37 | 73.68% | 1 | 29766 / 1698 |
| gpt-4o | $4.4921 | $0.025165 | 70.84 | 73.68% | 1 | 4114 / 1488 |

- Cost is the application estimate for provider-reported bulk and triage usage; ambiguous or unrecorded usage is excluded, so this is not a billing total.
- Cost per million is observed known cost divided by all reported input and output tokens combined, multiplied by 1,000,000; it is not a provider price tier.
- Requests counts recorded bulk chunk attempts only. Triage request counts are not persisted and are excluded; glossary lookup makes no provider requests.

[Final JSON](docs/measurements/2026-10-03-flores-en-de-matrix.json) and
[standalone matrix](docs/measurements/2026-10-03-flores-en-de-matrix.md) retain
per-model/per-job evidence. README gives reproduction commands. Each model
used a private database/cache with the same DOCX bytes and reference;
provider-side cached input may affect triage cost. ModelCostCalculator supplies
the application price snapshot. Mixed-token unit cost is the observed weighted
estimate, not a new provider rate. Unknown/uncheckpointed usage is excluded.

Final literal preservation was 14/19 for both models: placeholders 2/2, dates
2/2, currencies 0/4, numbers 10/11. Strict literal matching can penalize locale
formatting even when numeric meaning survives; it is not semantic correctness.
The suffixes provide synthetic field coverage and may slightly inflate chrF.
Short prose does not test realistic fields, tables, long documents, PDF reflow
or fallback pages. Population p95 and before/after parallelism stay unmeasured.

The first live run preceded two report-label fixes. Its untouched
[pilot JSON](docs/measurements/2026-10-03-flores-en-de-matrix-pilot.json) is retained:
mini chrF 69.8404, preservation 14/19, known cost $0.00399165;
4o chrF 72.8010, preservation 19/19, known cost $0.02498500.
The pilot's unqualified nested exclusion line refers to bulk-only `usage`;
`pipeline_usage` includes triage. Final CLI wording scopes that line and labels
reference versus back-translation mode. No formulas, prompts or numeric
accounting changed between runs. Score ordering reverses, so this tiny baseline
does not justify a model winner or a deployment-default change.

Four model measurement runs recorded $0.05823335 in known service usage estimates;
the final matrix alone totals $0.02925670. These are application estimates,
not provider invoice totals. Agent build-token accounting remains separate in
the DT-92 execution record. Local Ollama/GGUF is deferred in Future Work.


## 12. Decision record: triage fail-fast limits and visible analysis cost

Non-retryable provider failures stop triage retries immediately. Turn exhaustion
raises internal `TriageTerminalError`; the shared invalid-response catalog stays
retryable for bulk translation. Bare injected exceptions retain three attempts.
Every reported failed-attempt usage remains in cumulative totals, and failure
still publishes a degraded plan so jobs can proceed.

The agent uses validated `TRIAGE_MAX_TURNS` (default 8, 1–20) and
`TRIAGE_TIMEOUT_SECONDS` (default 60, finite >0, ≤300). The service adds five
seconds to the adapter timeout to preserve usage-bearing errors before outer
cancellation. MCP polling has its own limit. Remove the redundant tool-call cap;
with parallel calls disabled, the turn budget is the navigation-call bound.

The installed SDK generates per-run prompt cache affinity keys for supported
models. Existing pricing discounts cached input. Retention is deliberately
unset for short triage runs; this does not promise cache hits across documents.

Expose persisted `cost_usd_total` as document `analysis_cost_usd`, including
re-triage spend. Render it once per batch rather than multiplying document
expense by language jobs. Missing cost must not prevent the batch from rendering.
The value estimates reported usage and excludes unknown provider charges.


## 13. Decision record: measured triage convergence (DT-100)

DT-93 made turn exhaustion terminal to avoid paying up to three times for a
loop. The original plan described roughly threefold savings and a historical
third-attempt success; those are prior observations, not a controlled retry-policy
comparison. DT-100 addressed convergence before revisiting that policy.

Safe tool telemetry and a bounded, edge-weighted outline make the failure
observable and the first turn informed. Instructions name only the two real tools.
The default is now 16 turns (maximum configurable 20); tool output bounds and
all public/error contracts remain unchanged. The earlier eight-turn default in
the DT-93 record is historical.

Five live gpt-4o-mini documents at 16 turns produced accepted plans on 1/5 both
before and after. Mean triage usage estimates rose from $0.00244629 to $0.00689094.
The outline did not demonstrate a convergence gain. Three baseline complex-DOCX
runs all exhausted their budgets; searches dominated some runs, repeated reads
others. Keyword lengths alone cannot prove what the model searched for.

The owner approved a separate internal TRIAGE_MODEL setting, default gpt-4o,
while OPENAI_MODEL retains gpt-4o-mini for bulk/glossary. Terminal exhaustion stays
fail-fast and mini remains an explicit triage opt-in. Five live gpt-4o runs at
16 turns all produced accepted plans, averaging 3.8 requests and $0.03404600 per
document. This is more expensive triage than mini, but bulk's default is unchanged.
The five-file sample is not a representative success-rate measurement. The owner
also requested checking Luna6/5.6 alternatives; measurements are recorded in
[DT-100 execution](docs/plans/2026-10-03-triage-convergence-execution.md).

Both requested Luna candidates accepted 5/5 plans at 16 turns. Luna6 used 3.8 mean
requests and $0.000998178 with captured cache-write premium; Luna5.6 used 4.0 and
$0.002089002. They are tested opt-ins, not an automatic default replacement.
The five-file sample measures convergence, not classification or translation
quality. Production cost estimates exclude write premiums; the evaluation records
both estimates. Legacy provider/public contracts remain unchanged.


## DT-102: owner-selected Luna 6 triage default

After the DT-100 five-document comparison, the owner explicitly selected
TRIAGE_MODEL=gpt-6-luna as the default. Settings, real/fake fallback, Compose and
the environment example agree. OPENAI_MODEL remains gpt-4o-mini for bulk and
glossary. This reuses the measured Luna compatibility path and preserves explicit
model overrides, terminal exhaustion, 16 turns and public contracts. Existing
persisted analyses are not regenerated. The observed 5/5 convergence and roughly
$0.001 mean estimate are small-corpus evidence, not a population quality claim.
The production estimator still excludes cache-write premiums as documented.

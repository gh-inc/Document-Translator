# DECISIONS.md

**Status:** Living document. Decision records are written as decisions are
made; the measured numbers (cost per document, p95 latency, quality score)
are filled in at the end of implementation.

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

**Measured cost.** Final per-document cost, including retried and ambiguous
provider calls, is recorded per job in `chunk_attempts` and reported in §5.
Placeholder for the assessment sample document:
_"The real cost of translating the test document (X pages, Y chunks,
Z blocks) was $W.WW, including retry attempts."_

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
the core stays format-agnostic and testable with synthetic blocks; a third
format is one module + one registry entry. The costs, taken knowingly:
`format_metadata` is untyped across the core boundary (mitigated by
per-adapter schemas and per-format contract tests), and renderers depend on
the original file being available — acceptable on a single shared volume,
revisited under horizontal scaling (§4).

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
- Horizontal worker scaling (single worker + bounded async concurrency,
  measured in §3 before claimed)
- Auth / multi-tenancy
- Glossary editing UI (triage glossary is automatic)
- Sequential polish pass with translated context
- Side-by-side preview / in-place editing

---

## 6. Measured numbers

_Pending implementation. To be reported on a fixed sample document:_

- **Cost per document** — and what dominates it, including the retry share
  (`chunk_attempts` makes duplicate spend from ambiguous provider failures
  visible rather than hidden)
- **p95 chunk latency** and **p95 job latency** — before/after enabling chunk
  parallelism; numbers, not adjectives
- **Quality proxy** — back-translation chrF (EN→DE→EN vs. original), used as
  a coarse proxy for information preservation, not as a direct quality metric

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
FakeProvider only prefixes text. Actual translation quality, chrF, cost, and
latency measurements remain for the later measurement stage.

The renderer embeds PyMuPDF's bundled Droid Sans Fallback font buffer under a
private name so insertion uses the same glyph widths as fitting. A CJK alias
alone produced clipped Latin lines in review and was rejected. Glyph coverage
is checked before changing the canvas; unsupported glyphs yield a safe
`render_failed` error. Complex shaping, RTL, and pixel-perfect typography remain
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

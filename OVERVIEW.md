# OVERVIEW — Assessment Defense Brief

**Project:** Document Translator (async PDF/DOCX translation service)
**Assessment:** Software Engineer Assessment, ~3 days
**Document purpose:** a single file to read before defending the project.
It states what was built, what was measured, what was deliberately cut, and
where the honest weak points are. Nothing here is aspirational; every number
was observed in this repository.

---

## 1. The one-line answer

A document translation service where the LLM is treated as **one unreliable
dependency among several**: it is behind a port, cannot corrupt state, cannot
double-bill a committed result, and cannot hang the system. Upload a PDF, get
the same PDF translated, and kill the containers mid-flight — it resumes.

---

## 2. Hard requirements: where each one lives

| Brief requirement | Status | Where |
|---|---|---|
| Web interface, PDF in → translated PDF out | Met | FastAPI + React SPA; renderer re-opens the original as canvas |
| Real OpenAI API behind an interface | Met | `core/ports.py::LLMProvider`; `OpenAIProvider` / `FakeProvider` |
| Agents SDK with tool calling, agent earns its place | Met | **Triage only** — `read_blocks` / `search_blocks` navigation |
| MCP server usable from Claude Code / Cursor | Met | FastMCP streamable-http `:8001/mcp`, 4 workflow tools |
| ≥2 input formats | Met | PDF (PyMuPDF) + DOCX (python-docx) + Markdown (structurally bypassed) |
| Frontend | Met | React + Vite + TypeScript + Tailwind, Stark Future branding |
| `docker compose up --build` from fresh clone | Met | Multi-stage image, 3 services, shared volume |
| README: architecture, decisions, testing guide | Met | 10 sections + this overview |
| PROMPTS.md: AI usage incl. rejected output | Met | 32 dated log entries |
| DECISIONS.md: cuts, trade-offs, cost/p95, 3 more weeks | Met | 16 sections, live measurements |

---

## 3. Project scale (measured, not estimated)

| Metric | Value |
|---|---|
| Backend Python modules | 61 files, ~8 675 lines |
| Backend tests | **714 passed**, 2 live deselected (offline, `FakeProvider` only) |
| Frontend tests | **88 passed** (8 files) |
| Frontend source | 26 TS/TSX/CSS files |
| Commits | 93 |
| Backlog tasks completed | 108 (`DT-1`…`DT-109`) |
| `DECISIONS.md` decision records | 16 |
| `PROMPTS.md` log entries | 32 |
| Live provider tests | opt-in via `make test-live`; never run in CI |

The entire automated suite costs **zero dollars** — the fake provider makes
retries, resume, chaos and failure injection testable offline.

---

## 4. Architecture in one picture

```
Browser ──► web (FastAPI :8000)  ─┐
                                ├─► core services ─► adapters ─► shared /data
Claude Code ──► mcp (:8001/mcp) ─┘        │                        app.db (WAL)
                                          │                        uploads/
                                          └─► worker (claims, leases) out/
```

**The load-bearing idea:** two front doors, one core.
`api/` and `mcp_server/` are thin. They contain no business logic, no SQL, and
no duplicated rules. MCP calls `JobService`/`DocumentService` **directly** — we
explicitly rejected making MCP call the local REST API over HTTP, because that
would have added a network hop and duplicated error handling inside one
application.

**The second idea:** the queue *is* the database.
SQLite in WAL mode stores jobs, chunks, leases, attempts, and the translation
cache. There is no Redis and no broker. A `kill -9` leaves persisted leases;
the next worker expires them and resumes. Fewer moving parts is the reliability
lever, not a shortcut.

---

## 5. Measured results (live run, 2026-10-03)

Measured on the committed `samples/sample_en.pdf` (2 pages, 12 blocks) with
`LLM_PROVIDER=openai`, model `gpt-4o-mini`.

| Figure | Result |
|---|---|
| Forward cost EN→DE | **$0.00041715** (801 in + 495 out tokens) |
| Combined bulk spend (EN→DE→EN) | **$0.000825**; output tokens = 70.47% |
| Back-translation chrF | **85.27 / 100** |
| Number/Placeholder Preservation | **100% (5/5)** |
| Forward chunk latency | 6 645 ms (single observation) |
| Job latency enqueue→terminal | 7 108 ms / 6 211 ms |

**Reproduce:**
```bash
uv run python -m scripts.measure_quality samples/sample_en.pdf --env-file .env --timeout 240
```

### What we refused to fake

Seven figures are recorded as **"not measured"** with the reason, not filled
with plausible values:

- Population p95 job latency — two jobs cannot establish a p95.
- Before/after chunk parallelism — the sample has one chunk per job.
- Dates / currency / placeholders — the sample contains none of those tokens.
- Reference-based chrF — no independent German reference was supplied.
- Total provider bill — triage and ambiguous usage have no durable billing record.

This is deliberate. A stated gap reads as judgment; an invented number
destroys trust. The measurement script **exits non-zero under
`LLM_PROVIDER=fake`** so an offline run can never be mistaken for a
measurement.

---

## 6. The five decisions most likely to come up in defense

### 6.1 "Zero duplicate billing" — we rejected it as impossible
The initial design promised that a resumed job costs exactly the sum of unique
translations. That is physically impossible over an external API: if a timeout
fires *after* the provider processed the request, the client cannot know, and
Chat Completions has no idempotency key. We replaced the promise with an
explicit boundary:

- **Committed result:** exactly-once (`UNIQUE(translation_key, block_id)`).
- **Provider invocation:** at-least-once under ambiguity, measured in
  `chunk_attempts` and reported.

### 6.2 An agent where it earns its keep — and only there
**Where it earns its keep: triage.** A 400-page PDF does not fit in a context
window. The agent navigates the document (`read_blocks`, `search_blocks`),
decides what to inspect, and produces the plan that shapes every downstream
chunk. Tool calling is real and consequential.

**Where it would be an expensive `if`: bulk translation.** Mapping a fixed
prompt over N chunks needs determinism, parallelism, and predictable cost —
exactly what an agent loop destroys. Running N agent loops would be paying
model latency for control flow.

> *Note the correction:* the first draft gave the agent trivial tools
> (`detect_language`, `classify_domain`). Reviewers pushed back that delegating
> comprehension to a script is fake agency. We replaced them with navigation
> tools and let the model do the reading.

### 6.3 Opaque Metadata — the format abstraction we refused to build
We rejected a universal layout IR and a Markdown bridge. Instead the core sees
only `seq` + `source_text`; everything format-specific rides along as an opaque
JSON blob that only the owning adapter may read. Renderers **re-open the
original file as the canvas**, so images, headers, styles and fonts survive by
construction rather than reconstruction.

**Proof of extensibility:** DOCX was added as a second format implementing the
same two ports. The core never changed.

### 6.4 Single-job concurrency + soft cost cap
The worker processes exactly one job at a time (bounded parallelism only across
that job's chunks). This prevents OOM and keeps lease logic simple. The cost cap
is enforced by an in-memory `asyncio.Lock` reservation, deliberately a **soft
limit** — exact synchronization would serialize every LLM call. A small
overshoot is accepted and stated.

### 6.5 SQLite-only, no broker
The database *is* the queue. Single worker, leases, heartbeats. Horizontal
scaling is a stated cut, not an oversight — and the measurement backing that
claim is in the repo.

---

## 7. Resilience: the chaos test

`scripts/chaos-restart.sh` reproduces the reviewer's exact test: start a
translation, `kill -s KILL` the worker mid-flight, restart, verify recovery.

It proves the guarantee **from durable state**, using the `sqlite3` CLI against
`chunk_attempts`, `block_translations`, and `chunks` — not by adding
instrumentation to the fake provider.

**A subtlety worth demonstrating.** Reviewers initially expected the attempt
count to stay constant across a restart. That is mathematically impossible: a
chunk killed while `inflight` has no committed translation and *must* be
re-executed. So the script asserts the two invariants that are actually
provable:

1. `block_translations` never gains duplicate rows.
2. Chunks already `done` before the kill have not advanced their attempt count.

Both numbers are printed and labelled by which guarantee they demonstrate.

---

## 8. Honest weak points — expect these questions

1. **PDF fidelity is text-oriented only.** Complex tables, RTL, and heavy
   reflow are out of scope. The sample shows a 25% overflow-fallback rate
   (3 of 12 blocks moved to appended pages) — measured and reported, not hidden.
2. **Triage can leave a document in `analyzing` after `kill -9`.** Background
   tasks are in-process. Mitigation is an explicit `retry-triage` endpoint; the
   durable worker-owned triage queue is listed as future work.
3. **`/readyz` liveness is indirect.** No heartbeat table: it infers a dead
   worker from stale `inflight` leases. An idle system reports ready —
   absence of evidence is not proof of life. Documented, not papered over.
4. **The quality metric is a proxy.** Back-translation chrF measures information
   preservation and pipeline consistency, **not** translation quality. No
   reference-based (FLORES-style) score was produced.
5. **Single worker, local disk, Linux target.** No horizontal scaling, no object
   storage, no auth.
6. **DOCX drops inline formatting inside translated paragraphs** (one
   replacement run per paragraph, paragraph style preserved). Headers, footers
   and table cells are untranslated — a stated scope limit.
7. **Auth is absent.** Anyone on the network can use the service.

---

## 9. Quick wins — if there is spare time

Ordered by value-per-hour.

| # | Improvement | Why it pays off |
|---|---|---|
| 1 | **Reference-based chrF** on a 20–30 paragraph FLORES-style sample with an independent DE reference | Closes the weakest metric; the infrastructure already supports a reference file |
| 2 | **Reference sample with dates, currency, placeholders** | Turns today's `null` preservation categories into real numbers |
| 3 | **Multi-chunk sample** (30+ pages) | Produces a genuine p95 and a real parallelism before/after comparison |
| 4 | **Translate DOCX tables / headers / footers** | Removes a visible scope limit |
| 5 | **Playwright E2E for the UI flow** | Browser-level proof of the upload → progress → download path |
| 6 | **Retry-budget observability panel** | Surface internal retry budgets currently stored in `error_detail` |
| 7 | **Cost-cap as a hard limit** via reservation table | Removes the accepted soft-limit overshoot |
| 8 | **CI: image smoke test** — build + `/readyz` against the running container | Catches runtime-only breakage |

Items 1–3 are the highest value because they convert "not measured" into real
evidence, which is exactly what the brief rewards.

---

## 10. With three more weeks

Ordered by architectural impact, not by ease.

1. **Durable triage queue.** Move triage out of in-process background tasks into
   the worker's claim loop with leases. Eliminates the stuck-`analyzing` state
   and the manual retry endpoint.
2. **Object storage (S3/MinIO)** for uploads and outputs. Removes the shared
   disk that currently pins all three processes to one filesystem.
3. **Multi-worker queue semantics** (a real broker with visibility timeouts),
   unblocked by (2). Only then can the system scale horizontally.
4. **OCR / vision fallback** for scanned PDFs behind the same `Block`
   abstraction — `scanned_pdf` stops being a hard rejection.
5. **Glossary override UI** on top of the persisted `TranslationPlan`, plus a
   target-language term renderer (currently identity mapping).
6. **Sequential polish pass** using translated context for long-range style
   coherence — the mechanism we rejected for the parallel path, reintroduced
   deliberately as an optional post-pass.
7. **Auth / multi-tenancy** and **side-by-side preview** for editing before
   download.

---

## 11. Defending the collaboration itself

The brief asks for judgment, not prompt volume. The evidence lives in
`PROMPTS.md` (21 entries) and `DECISIONS.md` (11 records). Notable examples:

- We **rejected our own** "zero duplicate billing" guarantee as physically
  impossible over an external API.
- We **removed translated-context threading** from the parallel path: it was a
  serial dependency chain wearing a parallel costume.
- We **replaced** trivial agent tools with navigation tools when the reviewer
  identified them as fake agency.
- We **pushed back on the reviewer's own assumption** that attempt counts must
  not increase across a `kill -9` — and were correct: at-least-once for
  invocations and exactly-once for committed results is the only defensible
  reading.
- We **caught a branding error ourselves**: an early draft resolved "Stark"
  branding from `getstark.co`, a *different company*. Verified the real palette
  from `starkfuture.com` production CSS (`#FF1717`, `#000000`, `#242424`,
  `#1E1E1E`) and rejected hotlinking.

---

## 12. If you only remember five things

1. Two front doors, one core — MCP and REST call the same services directly.
2. The database is the queue; a `kill -9` resumes from persisted leases.
3. Committed translations are exactly-once; provider calls are at-least-once,
   and we say so instead of over-promising.
4. The agent is used for triage only, and we can explain exactly why.
5. Every number in `DECISIONS.md` is real or explicitly marked "not measured".

**On defense:** the brief's cost question ("what does one document cost you?")
has a precise answer in `DECISIONS.md` — **$0.00041715** forward, measured live.
Section 13 below is a *different* number: what building the service cost —
**~$7–8 in cash** (~$5 OpenCode Go quota for planning/architecture, ~$2–3
Gemini chat review), plus eight Codex build sessions metered at ~$7.28 in list
prices. Keep the two apart; conflating per-document cost with build cost is the
easiest mistake to make when asked about cost.

---

## 13. Development cost (AI-assisted)

The brief asks for "budget management" and "what your service costs per
document". This section is a different number and worth keeping separate: **what
it cost to build the thing**, measured across 8 agent sessions.

### Token usage and list-price equivalent

Priced at gpt-6.1-sol list rates: **$2.00 / $0.10 / $10.00** per million
tokens (input / cached input / output).

| Component | Tokens | Price / M | Cost |
|---|---:|---:|---:|
| New input | 854,883 (0.855M) | $2.00 | $1.71 |
| Cached input | 41,989,427 (41.99M) | $0.10 | $4.20 |
| Output | 136,621 (0.137M) | $10.00 | $1.37 |
| **Total** | | | **$7.28** |

**58% of the spend ($4.20 of $7.28) is cache reads.** That is the headline:
the largest token volume — 42M cached tokens against 0.86M fresh — cost less
than a quarter of what the fresh input cost in absolute terms.

### What prompt caching actually saved

Billed at the cached rate, those 41.99M tokens cost **$4.20**. At the
uncached input rate the same volume would have cost **$83.98**.

- **Saving: ~$79.78**, a **20x** discount factor ($2.00 → $0.10 per million).

### Cost per stage — the flat-average picture

Session totals are misleading on their own, because a session did not map
1:1 onto a stage: **several sessions covered two or three stages each**. The
per-stage view recorded in the staged roadmap is the meaningful one.

| Stage | Total tokens | vs. mean |
|---|---:|---:|
| Stage 8 — Frontend | 539K | **3.2x** |
| Stage 9 — E2E / chaos / measurements | 231K | 1.4x |
| Stage 4 — Worker | 179K | 1.1x |
| Stage 7 — MCP server | 174K | 1.0x |
| Stage 5 — REST API | 173K | 1.0x |
| Stage 6 — Triage agent | 166K | 1.0x |
| Stage 10 — Submission prep | 160K | 1.0x |
| Stage 3 — Formats | 139K | 0.8x |
| Stage 2 — LLM provider | 123K | 0.7x |

Mean across stages: **~168K tokens**. Seven of nine stages sit within
**0.7x–1.4x** of it. The distribution is flat; the frontend stage is the
single deliberate outlier at **3.2x**, and it was also the longest-running in
wall-clock time. That is an expected cost profile: a browser UI is the one
stage with visual iteration, a design system to derive, and a backend contract
extension (`GET /api/documents/{id}`) that had to be added along the way.

Per-session cached-token growth (sessions 3–8: 2.35M, 4.60M, 11.83M, 8.62M,
5.40M, 8.93M) is therefore **not** a cost-control failure — it tracks longer,
multi-stage sessions rather than any single stage becoming wasteful.

### Actual cash outlay — a necessary distinction

Actual out-of-pocket cost, **three separate channels**:

| Channel | Tool / provider | Cost | Note |
|---|---|---:|---|
| Agent build sessions | OpenAI Codex — gpt-6.1-sol / gpt-6-sol | ~$7.28 *list-price equivalent* | 8 sessions; see token tables above |
| Planning & architecture | **OpenCode Go** — different provider, **Kimi** models | **~$5** | Half the plan quota consumed |
| Ad-hoc review | Gemini Pro chat | **~$2–3** | Architecture and design consultation |
| **Cash total (the two subscription/chat channels)** | | **~$7–8** | |

**OpenCode Go is not OpenAI.** It is a separate tool, a separate provider, and a
separate model family (Kimi). Its ~$5 quota consumption is **not** part of the
$7.28 figure and must not be merged with it. Gemini is a third channel, also
outside $7.28.

Three distinct quantities are therefore in play — conflating any two is the
easy mistake:

- **$7.28** — metered-equivalent cost of the Codex agent sessions at OpenAI
  list rates (input + cached + output). A pricing valuation, not necessarily a
  separate invoice.
- **~$5** — OpenCode Go quota consumed on planning and architecture work
  (Kimi models, different provider).
- **~$2–3** — Gemini Pro chat consultations.

On defense the safe phrasing is: *"the build cost roughly $7–8 in cash across
two subscription channels — about $5 of OpenCode Go quota used on planning and
architecture, and $2–3 of Gemini chat review — plus eight Codex build sessions
whose metered usage works out to about $7.28 at list rates, 58% of it cache
reads."* Keep the channels labelled; a listener who hears "Codex" and "OpenCode"
as the same thing will assume the numbers were double-counted.

### The transferable lesson

The dominant lever is **session length, not model choice**. Cache reads are
already priced at 1/20th of fresh input; switching models changes the multiplier,
but scoping one session to one task is what actually reduces volume. Model
tiering (a stronger model for orchestration, a cheaper one for routine edits and
commits) is a secondary optimisation.

### Cross-check against the staged roadmap

The roadmap records per-stage OpenAI usage, entered by hand as each stage
closed. Summing all nine stages:

| Quantity | Roadmap (9 stages) | Session log (8 sessions) | Delta |
|---|---:|---:|---:|
| Input | 1 637K | 855K | roadmap is **1.9x** higher |
| Output | 245.8K | 136.6K | roadmap is **1.8x** higher |
| Total | 1 884K | 992K | roadmap is **1.9x** higher |

The two sources **do not reconcile**, and the roadmap recorded no per-stage
cached-token figures. That gap is now documented in the roadmap's
"Usage accounting notes", so a reader encounters the explanation where the
numbers live rather than discovering a silent omission. So:

- The **per-stage distribution** is reliable and is what "Cost per stage" above
  uses.
- The **$7.28 aggregate** rests on the session log alone. The cached portion
  (41.99M tokens) is **not independently corroborated** anywhere in the repo.

On defense, defend the *shape* (flat per-stage cost, one 3.2x frontend outlier,
cache-dominated spend) rather than defending $7.28 to two decimal places. If
asked for precision, say the aggregate is a session-log figure that the
per-stage notes were not reconciled against.

### Derived figures, recomputed

Two derived figures were recomputed from the token counts above:

- **Cache saving: ~$79.78** (41.99M tokens: $83.98 uncached vs. $4.20 cached).
- **Cached-input discount factor: 20x** ($2.00 → $0.10 per million).

Totals and shares were unaffected by rounding: $7.27 vs. $7.28, 57.7% vs. 58%
cache share, and $11.47 vs. ~$11.50 under an alternate $0.20 cached rate.
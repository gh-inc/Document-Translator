# Document Translator — Implementation Roadmap

> **Status:** approved, iterative execution.  
> **Workflow:** for each stage, produce a detailed implementation plan → validate the plan → hand it to the orchestrator for execution → verify.

---

## Stage 0 — Foundation & guardrails *(completed)*

**Goal:** working repository, tooling, guardrails.

**Deliverables:**
- `AGENTS.md`, `Makefile`, `pyproject.toml`, pre-commit config
- `.env.example`, `TASKS.md`, base `app/` skeleton
- Approved architecture, decisions, and AI-usage log drafts

**Exit criteria:** `make test`, `make lint`, `make typecheck` pass on the current skeleton; `docker compose up --build` starts without errors (empty services are acceptable at this point).

**Dependencies:** none.

---

## Stage 1 — Domain core + persistence layer

**Goal:** finalize core contracts and build the persistence adapter.

**Deliverables:**
- `app/core/models.py` per `docs/plans/2026-10-02-core-models.md` (strict Pydantic, 1:1 record-to-column mapping).
- `app/core/ports.py` per `docs/plans/2026-10-02-core-ports.md` (aggregate `create_job_with_chunks(job, chunks, chunk_blocks)`).
- `app/adapters/persistence/schema.sql` + persistence package per `docs/plans/2026-10-02-persistence-schema.md`.
- SQLite connection factory enforcing:
  - `PRAGMA journal_mode=WAL`
  - `PRAGMA synchronous=NORMAL`
  - `PRAGMA foreign_keys=ON`
  - `PRAGMA busy_timeout=20000`
- SQLite implementations of:
  - `DocumentRepository`
  - `JobExecutionRepository`
  - `TranslationCacheRepository`
- Filesystem `FileStorage` adapter.
- Contract tests for repositories against in-memory SQLite.

**Exit criteria:** all repository operations (create document/blocks/analysis, atomic job+chunks+chunk_blocks write, claim/heartbeat/complete, cache) are tested and green; `make test/lint/typecheck` passes.

**Dependencies:** Stage 0.

---

## Stage 2 — LLM provider layer

**Goal:** abstract LLM behind a port with testable and real implementations.

**Deliverables:**
- `app/adapters/llm/fake_provider.py` — deterministic pseudo-translation + configurable failure/latency.
- `app/adapters/llm/openai_provider.py` — real `LLMProvider` implementation.
- `app/adapters/llm/pricing.py` / `CostCalculator`.
- Port test-kit runnable against `FakeProvider` and `OpenAIProvider` (behind `@pytest.mark.live`).
- Integration with `ChunkRequest` / `ChunkResult`.

**Exit criteria:** port tests pass; live suite is opt-in; cost estimation is unit-tested; `make test/lint/typecheck` passes.

**Dependencies:** Stage 1.

**Can run in parallel with:** Stage 3.

Status: Completed
OpenAPI Usage:
  Token usage:         123K total  (108K input + 14.8K output)
  Context window:      56% left (121K used / 258K)

---

## Stage 3 — Format adapters: PDF + DOCX

**Goal:** extractor + renderer for two formats via the Opaque Metadata pattern.

**Deliverables:**
- `app/adapters/formats/pdf.py` — extract + render.
- `app/adapters/formats/docx.py` — extract + render.
- `app/adapters/formats/registry.py`.
- Opaque Metadata invariant test (core passes malformed metadata untouched).
- Sample document generator in `scripts/`.
- PDF renderer validation on a sample doc (overflow/fallback rate measurement).

**Exit criteria:** both formats pass upload → extract → render → output parses and contains translated text; `make test/lint/typecheck` passes.

**Dependencies:** Stage 1.

**Can run in parallel with:** Stage 2.

Status: Completed
OpenAPI Usage:
  Token usage:         139K total  (121K input + 17.7K output)
  Context window:      62% left (106K used / 258K)

---

## Stage 4 — Worker: claim loop, leases, executor

**Goal:** worker process that claims jobs/chunks from the queue, translates, and checkpoints.

**Deliverables:**
- `app/worker/claim_loop.py` — claim job, heartbeat, claim chunks.
- `app/worker/executor.py` — bounded-parallel executor (semaphore 8), retry/backoff/jitter.
- `app/worker/translation_loop.py` — cache lookup, LLM call, `INSERT OR IGNORE`, attempt recording.
- `app/worker/assembly.py` — transition to `assembling`, call renderer.
- Entrypoint `python -m app.worker`.
- Chaos/resume tests: kill worker, expire leases, restart, assert committed blocks are never re-translated.

**Exit criteria:** FakeProvider E2E upload → `done` passes; chaos test green; `make test/lint/typecheck` passes.

**Dependencies:** Stage 1 + Stage 2.

Status: Completed
OpenAPI Usage:
  Token usage:         179K total  (156K input + 22.7K output)
  Context window:      44% left (150K used / 258K)

---

## Stage 5 — REST API + SSE

**Goal:** FastAPI routers as a thin door over core services.

**Deliverables:**
- `app/api/main.py` with dependency injection.
- Endpoints:
  - `POST /api/documents`
  - `POST /api/jobs`
  - `GET /api/jobs/{id}`
  - `POST /api/jobs/{id}/retry`
  - `GET /api/jobs/{id}/events` (SSE)
  - `GET /api/jobs/{id}/download`
  - `GET /api/batches/{id}`
- `GET /healthz`, `GET /readyz`, `GET /metrics`.
- Structured errors: `{error_code, message, retryable}`.
- Integration tests via `httpx.AsyncClient`.

**Exit criteria:** all endpoints tested; SSE stream emits progress correctly; `make test/lint/typecheck` passes.

**Dependencies:** Stage 1 + Stage 4.

**Can be planned in parallel with Stage 4 if ports are stable, but E2E integration requires both.**

Status: Completed
OpenAPI Usage:
  Token usage:         173K total  (149K input + 23.2K output)
  Context window:      48% left (141K used / 258K)

---

## Stage 6 — Triage agent *(async, non-blocking)*

**Goal:** OpenAI Agents SDK analyzes the document in the background after upload.

**Deliverables:**
- `app/adapters/llm/triage_agent.py` with tools:
  - `get_text_sample`
  - `detect_language`
  - `classify_domain`
  - `extract_terminology`
- `POST /api/documents` triggers triage as a **background task** immediately after successful extract.
- Document status extended with `analyzing` so the UI/API knows the plan is being prepared.
- `POST /api/jobs` expects a ready `document_analyses` row; if analysis is not finished, returns `409 Conflict` with `error_code: analysis_pending`.
- Degraded fallback: if the agent fails after retries, fall back to a heuristic plan (`triage_status=degraded`, warning surfaced).
- Tests: fake agent; live test behind `@pytest.mark.live`; degraded-path test; non-blocking upload test.

**Exit criteria:**
- Upload returns immediately; analysis catches up in the background.
- `POST /api/jobs` does not invoke the agent runtime synchronously.
- Degraded path and cache (re-upload of the same document does not re-triage) are tested.

**Dependencies:** Stage 1 + Stage 2.

**Can run in parallel with:** Stage 4–5.

Status: Completed
OpenAPI Usage:
  Token usage:         166K total  (139K input + 27.2K output)
  Context window:      50% left (135K used / 258K)

---

## Stage 7 — MCP server

**Goal:** FastMCP streamable-http server usable from Claude Code / Cursor.

**Deliverables:**
- `app/mcp_server/server.py` using FastMCP.
- Tools:
  - `translate_file(path, target_languages[])`
  - `check_status(job_id)`
  - `download_result(job_id, output_dir)`
  - `list_recent_jobs(limit)`
- README: exact config + three-step verification recipe.
- Manual connection verification.

**Exit criteria:** MCP server starts; a document can be translated end-to-end from Claude Code without touching the web UI.

**Dependencies:** Stage 5.

Status: Completed
OpenAPI Usage:
  Token usage:         174K total  (149K input + 24.7K output)
  Context window:      41% left (156K used / 258K)

---

## Stage 8 — Frontend

**Goal:** React SPA with Stark branding.

**Deliverables:**
- `frontend/` — Vite + React + TypeScript + Tailwind.
- Views:
  - Upload
  - Job view with SSE progress and live cost
  - History
- Stark branding (verified starkfuture.com palette; local wordmark SVG, no hotlinked assets).
- Build to static; FastAPI serves static assets.
- Optional: Playwright integration tests if time allows.

**Exit criteria:** UI allows upload → view progress → download; styling matches Stark; `make dev` / `make up` works.

**Dependencies:** Stage 5.

Status: Completed
OpenAPI Usage:
  Token usage:         539K total  (484K input + 54.6K output)
  Context window:      85% left (48.7K used / 258K)

---

## Stage 9 — End-to-end, chaos, observability, measurements

**Goal:** everything works together; quality measurements and runbook.

**Deliverables:**
- Finalized `docker-compose.yml`; `docker compose up --build` works from a fresh clone.
- `scripts/chaos-restart.sh` — kills a container mid-translation and verifies resume.
- `scripts/measure_quality.py` — quality evaluation script:
  - Loads a sample document (20–30 paragraphs, ideally with reference translation, e.g. FLORES).
  - Translates EN→DE through the system.
  - Computes **chrF** against reference (if available) or **back-translation chrF** (EN→DE→EN vs. original).
  - Computes **Number/Placeholder Preservation %** — share of numbers, dates, currencies, and placeholder patterns (`{{...}}`, `%s`, etc.) preserved in translation.
  - Outputs JSON/CSV and prints numbers for `DECISIONS.md`.
- Prometheus `/metrics`, `/healthz`, `/readyz`.
- README section «3 a.m. runbook».
- Populate `DECISIONS.md` §5 measured numbers:
  - cost per document + retry share
  - p95 chunk latency, p95 job latency
  - chrF proxy
  - **Number/Placeholder Preservation %**

**Exit criteria:**
- `scripts/measure_quality.py` runs locally and produces reproducible numbers.
- Chaos script green; E2E through compose green.
- `DECISIONS.md` contains all measured numbers.

**Dependencies:** Stage 2–8.

Status: Completed
OpenAPI Usage:
  Token usage:         231K total  (195K input + 36.3K output)
  Context window:      32% left (180K used / 258K)

---

## Stage 10 — Finalization & submission prep

**Goal:** prepare for submission.

**Deliverables:**
- README polish: quickstart, architecture summary, testing guide, MCP config, 3 a.m. runbook.
- Verify `.env.example`; confirm no secrets committed.
- Fresh clone test:
  - `git clone` → `docker compose up --build` → upload PDF → get translated PDF.
- MCP verification from a clean Claude Code install using only the README.
- Complete the pre-submission checklist from `TEST_TASK.md`.
- Final `make test`, `make lint`, `make typecheck`.
- Git cleanup — squash/rebase (human PR prep, agent does not perform mutations).

**Exit criteria:** every item in `TEST_TASK.md` «Before Submitting» is checked.

**Dependencies:** Stage 9.

Status: Completed
OpenAPI Usage:
  Token usage:         160K total  (136K input + 24.6K output)
  Context window:      53% left (128K used / 258K)

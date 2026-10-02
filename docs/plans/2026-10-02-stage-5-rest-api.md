# Stage 5 — REST API + SSE Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expose a FastAPI REST surface and SSE progress stream that clients use to upload documents, create jobs, monitor progress, and download results.

**Architecture:**
- **Thin controllers.** Routers validate input, call core services, and map results to response schemas. No chunking, token counting, or job orchestration logic lives in `routers/jobs.py`.
- **Core service layer.** `app/core/services/job_service.py` owns job creation: chunking blocks by token budget, building the translation key, glossary, and calling the atomic `create_job_with_chunks` repository method.
- **Triage stub.** Until Stage 6 lands, `POST /api/documents` creates a default `DocumentAnalysisRecord` directly through the repository. This makes the full job-creation flow testable end-to-end.
- **Per-request SQLite connection.** Each API request gets its own `aiosqlite.Connection` via FastAPI dependencies, initialized with WAL pragmas, and closed after the response.
- **Structured errors.** A single exception handler maps `DocumentError`, `ProviderError`, and unexpected exceptions to `{error_code, message, retryable}`.
- **SSE with disconnect awareness.** The events endpoint polls the DB and yields `ServerSentEvent` messages; it checks `await request.is_disconnected()` inside the loop and breaks if the client disconnects.
- **Readiness without worker heartbeat.** `/readyz` checks only DB reachability and storage writability. Worker-heartbeat freshness is deferred to Stage 9.

**Tech Stack:** Python 3.12, FastAPI, `httpx` (tests), `prometheus-client`.

**Current State:**
- `app/api/` contains only `schemas.py`.
- `app/core/services/` does not exist.

---

## Task 1: FastAPI Application Factory and Dependencies

**Files:**
- Create: `app/api/main.py`
- Create: `app/api/dependencies.py`

`dependencies.py`:
- `get_settings()` → `Settings`.
- `get_db_connection()` → async generator yielding `aiosqlite.Connection` with WAL pragmas and schema loaded.
- `get_document_repo(connection)`, `get_job_repo(connection)`, `get_cache_repo(connection)`.
- `get_file_storage()`, `get_format_registry()`, `get_llm_provider()`, `get_cost_calculator()`.

`main.py`:
- Create `FastAPI` app.
- Include routers.
- Register exception handlers.
- Mount `/metrics`, `/healthz`, `/readyz`.

**Exit criteria:** A smoke test proves the app starts and `/healthz` returns 200.

---

## Task 2: Exception Handler and Structured Errors

**Files:**
- Create: `app/api/errors.py`

Map:
- `DocumentError` → HTTP 422, `{error_code, message, retryable: false}`.
- `ProviderError` with `retryable=True` → HTTP 503.
- `ProviderError` with `retryable=False` → HTTP 400/422.
- Generic exception → HTTP 500 with `{error_code: "internal_error", message: "Internal server error", retryable: true}`. Never leak tracebacks.

**Exit criteria:** Unit tests assert correct status codes and bodies.

---

## Task 3: Job Service

**Files:**
- Create: `app/core/services/job_service.py`

Implement `JobService`:

```python
class JobService:
    def __init__(
        self,
        document_repo: DocumentRepository,
        job_repo: JobExecutionRepository,
        cache_repo: TranslationCacheRepository,
        cost_calculator: CostCalculator,
    ) -> None: ...

    async def create_jobs(
        self,
        document_id: str,
        target_languages: list[str],
        idempotency_key: str,
    ) -> list[JobRecord]: ...
```

Responsibilities:
- Load document blocks and analysis.
- Build target-language glossary from analysis terms.
- Group blocks into chunks using `tiktoken` token counts (~800–1200 tokens, never splitting paragraphs/blocks).
- Build `ChunkRecord` and `ChunkBlockRecord` lists.
- For each target language, create a `JobRecord` and call `job_repo.create_job_with_chunks(job, chunks, chunk_blocks)`.
- Return the created job records.

**Exit criteria:** Unit tests prove correct chunk grouping and idempotency-key behavior.

---

## Task 4: Documents Router

**Files:**
- Create: `app/api/routers/documents.py`

`POST /api/documents`:
- Multipart upload.
- Validate file size cap (50 MB).
- Determine format via `FormatRegistry.resolve`.
- Save upload via `FileStorage.save_upload`.
- Extract blocks via `DocumentExtractor.extract` in a thread (adapters already thread blocking calls).
- Create document record and blocks via `DocumentRepository`.
- **Triage stub:** create a default `DocumentAnalysisRecord` via `DocumentRepository.save_analysis` with:
  - `source_language="en"` (or simple statistical detection if trivial to add)
  - `domain="general"`
  - `register="neutral"`
  - empty terms/warnings
  - `triage_status=TriageStatus.OK`
- Return `DocumentUploadResponse`.

**Exit criteria:** Upload test with sample PDF/DOCX returns expected response and persisted blocks + analysis.

---

## Task 5: Jobs Router

**Files:**
- Create: `app/api/routers/jobs.py`

`POST /api/jobs`:
- Validate request.
- Delegate to `JobService.create_jobs(...)`.
- Return `BatchResponse`.

`GET /api/jobs/{id}`:
- Load job via repository.
- Return `JobSummaryResponse`.

`POST /api/jobs/{id}/retry`:
- Load job.
- Reset chunks with fatal attempts back to `pending`.
- Optionally accept `raised_cost_cap_usd`.
- Return `JobSummaryResponse`.

`GET /api/jobs/{id}/events`:
- SSE stream.
- Inside `while True`:
  - `if await request.is_disconnected(): break`
  - Poll job every second.
  - Yield `ServerSentEvent`.
  - Break on terminal state.

`GET /api/jobs/{id}/download`:
- If job not terminal, return 409.
- Return rendered file from `FileStorage.get_output_path`.

`GET /api/batches/{id}`:
- Return `BatchResponse` with all jobs in the batch.

**Exit criteria:** Router tests cover all endpoints; SSE test verifies disconnect handling.

---

## Task 6: Health, Readiness, and Metrics

**Files:**
- Create: `app/api/routers/health.py`

`GET /healthz`:
- Always 200.

`GET /readyz`:
- Check DB connection (execute `SELECT 1`).
- Check storage writable (create and remove a temp file in `upload_storage_path`).
- Worker-heartbeat check is intentionally omitted for Stage 5.

`GET /metrics`:
- Prometheus metrics endpoint using `prometheus_client`.
- Initial metrics: `jobs_by_status` gauge, `llm_cost_usd_total` counter, `llm_errors_total` counter, `cache_hits_total` counter.

**Exit criteria:** `/healthz` 200, `/readyz` 200, `/metrics` returns Prometheus text.

---

## Task 7: Integration Tests

**Files:**
- Create: `tests/api/test_api.py`

Use `httpx.AsyncClient` with FastAPI `TestClient`/`AsyncClient`.

Tests:
- Upload PDF/DOCX → 200, document + analysis exist.
- Create job → 200, chunks created.
- Get job status → 200.
- SSE stream reaches terminal state and disconnects cleanly.
- Batch endpoint.
- Error cases: unsupported file, corrupt file, scanned PDF.

**Exit criteria:** All integration tests pass with `FakeProvider` and in-memory SQLite.

---

## Task 8: Verification

```bash
make test
make lint
make typecheck
```

Expected: all green.

---

## Task 9: Commit and Backfill `TASKS.md`

Proposed task IDs:
- `DT-41`: FastAPI app factory, dependencies, error handlers
- `DT-42`: `JobService` with chunking logic
- `DT-43`: documents router + triage stub
- `DT-44`: jobs router + SSE
- `DT-45`: health/readiness/metrics + API integration tests

Single-commit option:

```bash
git add app/api app/core/services tests/api TASKS.md
git commit -m "DT-41: feat(api): implement REST API, SSE, and job service"
```

---

## Critical Agent Reminders

1. **No fat controllers.** Chunking, token counting, and job orchestration live in `app/core/services/job_service.py`, not in `routers/jobs.py`.
2. **Triage stub.** `POST /api/documents` writes a default `DocumentAnalysisRecord` synchronously so the full API flow works before Stage 6.
3. **SSE disconnect handling.** The SSE generator must check `await request.is_disconnected()` inside the polling loop and break to free resources.
4. **No worker heartbeat in `/readyz`.** Check only DB and storage. Worker heartbeat freshness is deferred to Stage 9.
5. **Per-request DB connection.** Do not share one SQLite connection across requests; use FastAPI dependency injection.
6. **Structured errors.** Never leak raw exceptions or tracebacks to clients.

## Stage 5 execution record

The user authorized implementation and commits on 2026-10-02. Work takes place
on `stage-5-rest-api`; the starting suite passed 273 tests (one live deselected).
Task IDs are DT-31–DT-35, following the actual backlog rather than the provisional
DT-41–DT-45 in this plan.

Delegation uses three implementation agents with disjoint ownership:
job service/internal persistence/retry coordination, document service/router,
and jobs/batches/SSE/integration tests. The orchestrator owns factory,
dependencies, safe errors, health/metrics, documentation, reviews and commits.
The architecture, existing REST schemas, core records/ports and DDL are the
approved contracts. User edits to PROMPTS.md, TEST_TASK.md and docs/roadmap.md
are preserved and excluded from this delivery.

### Rulings and corrections

- SQL for health, metrics, idempotency and retry stays in the persistence
  adapter. Document and retry writes occur in service-initiated transactions;
  aggregate enqueue retains its own atomic transaction.
- Controllers delegate ingestion and orchestration to core services. Format
  extractor coroutines are awaited directly because adapters already thread
  their blocking operations. Tokenization runs in a thread.
- Tests use temporary file-backed SQLite to exercise real WAL and separate
  request connections. An isolated `:memory:` connection per request would
  lose the database between requests.
- A repeated request completes a partially created language batch. Family
  validation is serialized with each aggregate insertion so concurrent
  conflicting language sets cannot poison the same idempotency key.
- Analysis terms carry source-term identity mappings until target-language
  triage is available. The existing worker computes the semantic translation
  key from persisted language/model/prompt/glossary values.
- Retry preserves successful cache rows and billed attempts. Internal JSON in
  the existing diagnostic field carries an increased cap and attempt baselines
  across restarts. It is never exposed as a client error. Public schema and
  record models are unchanged.
- Downloads require `done` or `completed_with_errors`; a failed terminal job
  does not have a valid rendered artifact. Unknown stored error codes map to
  the safe catalog rather than exposing diagnostic strings.
- Prometheus cost/error totals come from durable state, so repeat scrapes do
  not multiply totals. The initial cache-hit counter is zero; durable hit
  instrumentation is deferred because no approved persistence field stores it.
- Unsupported extensions use 415, oversized uploads use 413, corrupt/scanned
  files use 422. All return the approved error envelope. Framework validation
  and 404 errors use that envelope too.

### AI usage and review

Implementers work without commits or additional agents. The orchestrator reviews
interfaces and changes, then dispatches an independent whole-stage review.
Rejected initial approaches include strict full-batch validation (which blocked
recovery after a partial enqueue), adapter-owned ordinary retry transactions,
and resetting billed attempt rows on retry. They are corrected by subset-aware
batch recovery, service-owned transactions, and persistent retry baselines.
Operational limits and startup instructions are documented in `docs/api.md`.

Independent review identified two upload limits from Architecture's Pipeline
that the plan omitted: 400 pages and extracted-text size. They are enforced
before persistence, with the text cap set to 10 MiB of UTF-8 source text;
regressions cover rejection and cleanup. Size/page errors use architecture's
`size_limit`/`page_limit` catalog values.

### Final verification and review

All eight implementation/verification tasks are complete. Final checks:
`make test`: 302 passed, one live test deselected; `make lint`: clean, 105 files
formatted; `make typecheck`: clean, 44 source files. No live OpenAI calls ran.
The two existing Pydantic `register` warnings remain. Threaded test commands
were run outside the sandbox after sandboxed I/O stalled.

Independent review confirmed page/text rejection and a second defect: cancelling
an upload during a threaded write could leave a published orphan. The service
now shields publication, waits for it to settle and completes cleanup before
propagating cancellation. A delayed-thread regression verifies repeated
cancellation leaves no artifact or document row. A scoped re-review found both
issues resolved with no new blockers. Range request errors also use catalogued
JSON while successful partial downloads retain HTTP 206 behavior.

Optional future work: enforce a request-body cap before multipart parsing;
currently the framework parses/spools the upload before the bounded-file read
rejects it. This does not change the accepted file size limit.

The first commit attempt was rejected by the pre-commit Ruff version for two
UP038 syntax checks that the installed `make lint` Ruff accepted. Both checks
now use `isinstance(value, int | float)`; hooks remain enabled.

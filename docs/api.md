# REST API and SSE

Run `make dev` for the FastAPI process on port 8000. Run
`uv run python -m app.worker` separately with the same storage/database settings.
For offline work, set `LLM_PROVIDER=fake`. Open `/docs` for the existing request
and response schemas. Startup initializes SQLite WAL and the schema before the
server accepts requests. Every request opens and closes its own configured
connection; streaming requests retain their connection until the stream ends.

## Upload and translate

1. Upload a PDF or DOCX using multipart field `file` at `POST /api/documents`.
   Limits are 50 MiB for uploads, 400 PDF pages and 10 MiB of extracted UTF-8 text. The response includes the document ID, block count and `warnings`.
   PDF glyph warnings identify unsupported Unicode codes without quoting document
   text. They are advisory: upload proceeds. Warnings are returned on uploads,
   including duplicate uploads, and are not persisted; readiness and retry-triage
   responses have an empty `warnings` list. DOCX has no PDF font coverage warning.
   Filenames are sanitized; suffix and signature must agree. Failed ingestion
   rolls back database records and removes the saved upload.
2. Poll `GET /api/documents/{id}` from the client until `status=extracted`.
   This read returns the existing upload response shape: `id`, `filename`,
   `format`, `status`, and the persisted `block_count`. It reports `analyzing`
   while triage is pending and `failed` if document processing failed. Stop
   readiness polling on `failed`; unknown IDs return the catalogued 404
   `{error_code, message, retryable}` envelope. The endpoint does not start
   triage or wait for it to finish.
3. Once extracted, send the document ID, target languages and a client-selected
   idempotency key to `POST /api/jobs`:

   ```json
   {"document_id": "<document-id>", "target_languages": ["de", "fr"], "idempotency_key": "request-1"}
   ```

   The response contains one batch ID and one queued job per language. Repeating
   the request returns the same persisted jobs. A conflicting reuse of the key
   returns 409. Job creation groups whole blocks into about 1000-token chunks;
   a single larger block occupies its own chunk. Each job aggregate is atomic.
4. Poll `GET /api/jobs/{id}` or connect to `GET /api/jobs/{id}/events`.
   SSE uses the existing `ServerSentEvent` JSON contract, polls once per second,
   stops at terminal status and checks for client disconnects every iteration.
   `GET /api/batches/{id}` returns the batch's jobs.
5. Download via `GET /api/jobs/{id}/download` when status is `done` or
   `completed_with_errors`. The latter may contain original text for blocks
   whose translation failed or whose translated PDF text contains visible glyphs
   unavailable in the bundled renderer font. Those PDF blocks retain their
   original canvas text; supported blocks still receive translations. Nonprinting
   controls, format characters and Unicode variation selectors are removed from
   rendered translations while newlines and tabs survive. Other states return 409.

Uploads return `status=analyzing` after extraction, before background triage
finishes. The agent navigates text with `read_blocks` and `search_blocks` and
persists a language/domain/register/terminology plan using its own SQLite
connection. `LLM_PROVIDER=fake` selects deterministic offline triage and reuses
`FAKE_FAIL_RATE`, `FAKE_FAIL_MODE` and `FAKE_LATENCY_MS`. Provider analysis has
three attempts, each limited to 60 seconds, with bounded navigation and SDK
retries disabled. Exhausted attempts publish a heuristic plan with
`triage_status=degraded`, an uncertainty warning, and `status=extracted`.

`POST /api/jobs` returns `409 analysis_pending` with `retryable=true` until the
document is extracted and an analysis exists. Clients poll the document status
endpoint for readiness before submitting jobs. Successful
analysis terms become identity glossary entries until translated terminology
is available. A repeated upload with identical bytes returns the original
document ID and analysis, even with a different filename. IDs for new uploads
are SHA-256 content digests; older UUID documents remain usable but are not
retrospectively deduplicated.

If the web process dies during analysis, use
`POST /api/documents/{id}/retry-triage` (no body). This explicit request returns
the existing upload response with `status=analyzing` and schedules fresh work.
It also replaces a degraded analysis atomically when analysis succeeds, provided
no translation jobs exist yet. Once any job exists, the plan is immutable;
retrying degraded analysis returns `409 conflict` to preserve resumed-job and
translation-cache consistency. A successful analysis is reused; duplicate scheduled tasks skip completed work.
Missing documents return 404; failed extraction cannot be retried through
triage. Background tasks are in-process and are not automatically restarted.
REST and MCP share per-document advisory filesystem locks on the local data
volume. A conditional SQLite update claims eligible triage before background
scheduling; only the owner schedules analysis. Process death releases the lock
and a new MCP submission or explicit REST retry recovers abandoned analysis.
Request cancellation also releases ownership. Shared upload locking protects
content-addressed artifacts across the two processes; provider analysis does not
hold that upload lock, a database connection, or a transaction.
Storage/DB failures can leave `analyzing`
for explicit recovery. Empty extracted content is marked `failed/corrupt_file`.
SDK tracing is disabled; raw exceptions and document text are never logged.

`GET /api/jobs?limit=10` returns recent job summaries, newest first with stable
ID ordering for equal creation timestamps. The shared service clamps integer
limits to 1–100; malformed REST query parameters return a catalogued 422 error.
An empty collection is `[]`. MCP calls the same service directly.

## Retry and costs

`POST /api/jobs/{id}/retry` requeues terminal failed or partially completed jobs.
It keeps successful translations and billed attempt history, resetting only
chunks with missing translations. It grants a fresh attempt budget for those
chunks. An active job/lease cannot be retried. The optional
`raised_cost_cap_usd` must be finite and exceed the configured default cap and
spend already recorded.

Retry settings are stored internally in the existing `jobs.error_detail` field
while `error_code` is empty. Workers read the raised cap and per-chunk attempt
baselines across process restarts. This internal policy is never returned as a
client error. Public records, repository ports and database schema are unchanged.
Provider invocation remains at least once under ambiguous failures; all known
billed usage remains counted. A download already obtained before retry may
represent the earlier partial result; fetch again after the retry completes.

## Errors and operations

Errors use `{error_code, message, retryable}` from the shared catalog, including
validation, missing routes/resources, provider and unexpected errors. Responses
never include exception details or persisted diagnostic text.

- `/healthz` returns 200 without dependency access.
- `/readyz` checks database reachability and writability of upload/output
  directories, and stale `inflight` chunk leases. Lease expiry older than
  `max(120, 2 * CHUNK_LEASE_SECONDS)` seconds yields catalogued `not_ready`
  (503). With no inflight chunks, an absent worker cannot be detected.
  A paused/slow worker can remain not ready until lease recovery.
- `/metrics` exports job counts per status and known provider cost/error totals
  from durable data. Bulk `llm_cost_usd_total` remains job expense;
  `llm_triage_cost_usd_total` and `llm_triage_tokens_total{direction="input|output"}`
  report cumulative document-level triage expense, including reported retries.
  One analysis is charged once across a multi-language batch. Repeated scrapes
  do not accumulate totals again, and totals survive restarts. Analysis current
  usage is overwritten on re-triage; cumulative usage survives degraded
  delete-and-republish. Historical rows migrate with zeros; old triage expense
  and unknown or uncheckpointed provider usage cannot be reconstructed.
  `cache_hits_total` is initialized to zero; durable cache-hit instrumentation
  is deferred because the approved schema has no cache-hit record.

Use `make test`, `make lint` and `make typecheck` for offline verification.
API tests run against temporary file-backed SQLite databases with real WAL,
separate request connections, real format adapters and `FakeProvider`.

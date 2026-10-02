# Document Translator

Async PDF/DOCX translation with persisted triage, independent jobs per language,
SQLite WAL checkpoints and a translation cache. REST and MCP call the same core
services; a separate worker translates and renders the original document.
See [architecture](ARCHITECTURE.md), [REST usage](docs/api.md),
[worker operations](docs/worker.md) and [trade-offs](DECISIONS.md).

## Compose quickstart

From a clean clone with Docker and Compose installed:

```bash
docker compose up --build -d --wait
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/readyz
```

Open `http://localhost:8000` to upload a PDF or DOCX; MCP listens on
`http://localhost:8001/mcp`. The stack defaults to `LLM_PROVIDER=fake` and
requires no API key. Three non-root processes share one named `/data` volume;
only the dedicated `MCP_HOST_SHARED_DIR` is bind-mounted for editor files.
`docker compose down` keeps documents and results; deleting the volume deletes
those records. `make up`, `make down`, and `make logs` wrap Compose commands.

For live translation copy `.env.example` to `.env`, configure the key, set
`LLM_PROVIDER=openai`, and recreate the services with `docker compose up -d`.
Keep `.env` local. The fixed upload limits are enforced by the services, rather
than configurable `MAX_FILE_SIZE_MB` or `MAX_PAGES` environment variables.

Run the reproducible offline crash check:

```bash
./scripts/chaos-restart.sh
```

It creates an isolated Compose project, kills the worker while chunks remain,
then compares durable SQLite checkpoints after restart. See
[operator notes](docs/ops.md) for prerequisites, `--keep`, counters and the
at-least-once boundary for interrupted provider requests.

## Local offline quickstart

Use Python 3.12 and `uv`. From the repository root:

```bash
uv sync
mkdir -p /tmp/document-translator/data /tmp/document-translator/mcp-files/input /tmp/document-translator/mcp-files/output
export DATABASE_PATH=/tmp/document-translator/data/app.db
export UPLOAD_STORAGE_PATH=/tmp/document-translator/data/uploads
export OUTPUT_STORAGE_PATH=/tmp/document-translator/data/out
export MCP_SHARED_DIR=/tmp/document-translator/mcp-files
export LLM_PROVIDER=fake
cp samples/sample_en.pdf /tmp/document-translator/mcp-files/input/
uv run python -m app.mcp_server
```

The MCP server listens on `0.0.0.0:8001/mcp`. In another terminal, export the
same database/storage/provider settings and run `uv run python -m app.worker`.
For REST, start `make dev` in a third terminal (API docs at
`http://localhost:8000/docs`). The fake provider makes deterministic offline
translations. For real translation choose `LLM_PROVIDER=openai` and configure
the API key through application Settings; `.env.example` lists placeholders.
Environment variables must be exported; application Settings do not automatically
load `.env`. Compose loads `.env` and passes supported settings to every process.

## Frontend development

Use Node.js 24 or newer and npm alongside Python 3.12 and `uv`. Install the
locked frontend dependencies from the repository root:

```bash
npm --prefix frontend ci
```

Export the database/storage/provider settings from the offline quickstart in
each backend process's terminal. Start the two web development servers:

```bash
# Terminal 1: FastAPI
make dev
```

```bash
# Terminal 2: Vite
make frontend-dev
```

Open the URL printed by Vite (normally `http://localhost:5173`). Vite proxies
`/api` requests, including progress streams, to FastAPI on port 8000; browser
requests use the Vite origin, so no CORS configuration is needed. Translation
also requires a separate worker process: in another terminal, export the same
database/storage/provider settings and run `uv run python -m app.worker`.
The MCP server is optional for browser use. With `LLM_PROVIDER=fake`, this
workflow runs offline.

Upload a PDF or DOCX and select target languages. The UI waits for analysis by
polling `GET /api/documents/{id}`; once the document is `extracted`, it submits
one `POST /api/jobs` request. Readiness polling stops after 60 seconds and offers
**Retry analysis**, which calls `POST /api/documents/{id}/retry-triage` before
checking readiness again. **Cancel** stops the browser's current submission and
polling; it does not cancel persisted backend work. Job and history views show
progress, cost, downloads and **Retry translation** for failed or partial jobs.

## Serve a production frontend build

From the repository root:

```bash
npm --prefix frontend run build
make dev
```

The build writes `frontend/dist`. FastAPI serves it at `http://localhost:8000`,
including SPA navigation such as `/history`; the same origin serves `/api`,
so no CORS configuration is needed. Its custom 404 handler keeps unknown
`/api` routes as structured JSON errors instead of returning the SPA HTML.
`make dev` uses the local reload server to verify the production build; the
worker remains required with the same database/storage/provider settings.
The container image builds this bundle in its Node stage and serves it from FastAPI.

## Connect an editor

For Claude Code:

```bash
claude mcp add --transport http stark-translate http://localhost:8001/mcp
claude mcp list
```

Start a fresh Claude Code session and use `/mcp` to inspect the connection.
For an isolated session without changing stored MCP configuration, put this JSON
in a temporary file and launch
`claude --strict-mcp-config --mcp-config /path/to/mcp.json`:

```json
{
  "mcpServers": {
    "stark-translate": {
      "type": "http",
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

In Cursor, add the following to the project's `.cursor/mcp.json`, then enable
the server in MCP settings:

```json
{
  "mcpServers": {
    "stark-translate": { "url": "http://localhost:8001/mcp" }
  }
}
```

These configurations follow the official
[Claude Code MCP guide](https://code.claude.com/docs/en/mcp) and
[Cursor MCP guide](https://cursor.com/docs/mcp).

## Three-step MCP verification

Before the container verification, create the dedicated directories and grant
UID 10001 write access to `output/` (or use a local ACL). For example,
`mkdir -p mcp-files/input mcp-files/output` and `chmod 0777 mcp-files/output`
permit downloads in that dedicated output directory. Copy the sample into
`mcp-files/input/`; host input files must be readable by the container.

1. Place `sample_en.pdf` in the shared `input/` directory. Ask the editor to call
   `translate_file` with `path="input/sample_en.pdf"` and
   `target_languages=["de"]`. It returns `document_id`, `batch_id`, `job_ids`.
2. Call `check_status(job_id="<returned-job-id>")` until `status="done"` or
   `"completed_with_errors"`. The result includes progress, cost and safe errors.
   `list_recent_jobs(limit=10)` returns recent jobs, newest first; limits clamp
   to 1–100. REST exposes the same list at `GET /api/jobs?limit=10`.
3. Call `download_result(job_id="<returned-job-id>", output_dir="output")`.
   Open the returned path under the shared directory. Partial completion retains
   source text for untranslated blocks; inspect errors before using the result.

`translate_file` waits at most 45 seconds for triage. If analysis is still
pending, it returns `{error_code, message, retryable, document_id}` with
`error_code="analysis_pending"` and `retryable=true`. Call
`check_status(job_id="<document-id>")` later. Once the document is extracted,
repeat `translate_file` to enqueue jobs. Identical content and normalized
language sets reuse the same MCP jobs. Translation itself runs in the worker.
Tool failures are structured results; clients should inspect `error_code`.
At the protocol level, FastMCP wraps union/list results in
`structuredContent.result`; success/error models above describe that value.

## Shared directory and triage ownership

Relative and absolute MCP input/output paths must resolve inside
`MCP_SHARED_DIR`. Traversal, symlink escapes and non-file inputs are rejected.
The server can access only this configured mount through its tools. Keep
`input/` and `output/` beneath it. Internal uploads and worker artifacts remain
in their separate storage directories.

For the container stack, `MCP_HOST_SHARED_DIR` is the host bind source
(default `./mcp-files`); `MCP_SHARED_DIR=/mcp-files` is the container destination.
Mount only that dedicated directory. Host paths such as `/home/.../file.pdf`
will not work inside the container; use `input/file.pdf` or `/mcp-files/input/file.pdf`.

REST and MCP claim triage atomically before scheduling work. A shared advisory
lock excludes concurrent analysis and is released on cancellation or process
death, permitting a new submission/retry to recover an abandoned `analyzing`
record. Successful analysis is reused and analysis already used by jobs stays
immutable. Processes must share the same local database/data filesystem;
this deployment targets Linux with local SQLite WAL storage.

## Tests and operations

```bash
make test
make lint
make typecheck
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Offline tests use fake providers, real temporary WAL databases, FastMCP's
in-memory client and worker-produced PDF/DOCX artifacts. Real OpenAI tests
require explicit `make test-live` and configured credentials.

## 3 a.m. runbook

Start with `docker compose ps`, `/healthz`, `/readyz`, and `/metrics`:

```bash
curl -fsS http://localhost:8000/metrics
docker compose logs --tail=100 worker web mcp
```

| Symptom | Check | Safe action |
| --- | --- | --- |
| Jobs remain queued | `docker compose ps worker`; compare `DATABASE_PATH`, `UPLOAD_STORAGE_PATH`, `OUTPUT_STORAGE_PATH` in your Compose configuration across processes | Start/recreate the worker with `docker compose up -d worker`; use the same `/data` volume. An idle `/readyz` 200 alone does not prove worker liveness. |
| Jobs remain running | Inspect chunk leases using the SQL below and worker logs; normal job/chunk leases last 60 seconds and heartbeat runs every 10 seconds | Restart a dead worker and allow the outstanding job/chunk leases to expire; committed chunks resume from cache. Avoid editing states directly. |
| Cost rises | `/metrics` exposes `llm_cost_usd_total`; query attempts by outcome and retry number below | Check retries and `MAX_COST_PER_JOB_USD`; reduce concurrency if rate limited, correct provider failures, and stop accepting new work while investigating. Recorded spend excludes unknown usage and triage calls. |
| Partial results | Job status `completed_with_errors` means missing translations rendered as source text; inspect `GET /api/jobs/{id}` errors | Resolve the reported cause; `curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' http://localhost:8000/api/jobs/<job-id>/retry` requeues only missing work. Download again after completion. |
| Triage remains analyzing | `GET /api/documents/{id}` and web/MCP logs; analysis tasks run in-process | After a crashed owner, `curl -fsS -X POST http://localhost:8000/api/documents/<document-id>/retry-triage`; successful analysis and analysis already used by jobs remain immutable. |

Read durable diagnostics without changing state:

```bash
docker compose exec -T web sqlite3 /data/app.db "SELECT job_id,seq,status,lease_expires_at FROM chunks ORDER BY job_id,seq;"
docker compose exec -T web sqlite3 /data/app.db "SELECT outcome,COUNT(*),SUM(cost_usd) FROM chunk_attempts GROUP BY outcome;"
docker compose exec -T web sqlite3 /data/app.db "SELECT SUM(cost_usd),SUM(CASE WHEN attempt_no>1 THEN cost_usd ELSE 0 END) FROM chunk_attempts;"
```

`/healthz` is dependency-free web liveness. `/readyz` checks the database,
writable storage, and stale `inflight` chunk leases: 503 appears when lease
expiry is more than `max(120, 2 * CHUNK_LEASE_SECONDS)` seconds in the past.
An idle deployment reports ready even if the worker is absent; a paused worker
can temporarily report not ready until it recovers. Worker container health
checks process liveness; MCP health checks its transport. Neither supplies a
separate HTTP `/healthz` endpoint.

Metrics currently expose durable job counts and known cost/error totals.
`cache_hits_total` is zero because hits are not persisted; latency distributions
and retry spend are obtained from SQLite by the measurement command. Unknown
usage from a killed or timed-out invocation cannot be reconstructed from a
missing attempt row. See [operator notes](docs/ops.md) and
[worker operations](docs/worker.md).

## Live quality and cost measurements

This explicit command uses the real pipeline in isolated temporary storage:

```bash
uv run python -m scripts.measure_quality samples/sample_en.pdf --env-file .env
uv run python -m scripts.measure_quality samples/sample_en.pdf --env-file .env --reference /path/to/reference.txt
```

Set `LLM_PROVIDER=openai` in the chosen environment file; specify another target
with `--target-language fr`. The command rejects a
fake provider and missing credentials before work starts, emits JSON on stdout
and a readable table on stderr. Without a reference it labels chrF as
back-translation, a coarse preservation proxy. Recorded cost comes from bulk
attempts; triage and unknown transport usage have no durable billing record.
A single job duration is one observation and cannot establish a population p95
or a parallelism comparison. Measured results and explicit gaps are recorded in
[DECISIONS.md](DECISIONS.md).

# Document Translator

Async PDF/DOCX translation with persisted triage, independent jobs per language,
SQLite WAL checkpoints and a translation cache. REST and MCP call the same core
services; a separate worker translates and renders the original document.
See [architecture](ARCHITECTURE.md), [REST usage](docs/api.md),
[worker operations](docs/worker.md) and [trade-offs](DECISIONS.md).

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
load `.env`. Docker/Compose delivery is scheduled for Stage 9.

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
Container deployment and the Compose-based `make up` delivery check remain
scheduled for Stage 9.

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

For the later container stack, `MCP_HOST_SHARED_DIR` is the host bind source
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

If a job remains queued, check that the worker uses the same database settings.
If triage remains pending after process failure, resubmit through MCP or use
`POST /api/documents/{id}/retry-triage`. Check `/readyz` for database/storage
availability and `/metrics` for durable job counts and known costs. Forced
worker termination waits for persisted leases to expire before recovery;
committed translations are cached. More details: [worker operations](docs/worker.md).

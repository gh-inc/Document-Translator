# Document Translator

Async PDF/DOCX/Markdown translation with persisted triage, independent jobs per language,
SQLite WAL checkpoints and a translation cache. REST and MCP call the same core
services; a separate worker translates and renders the original document.
See [architecture](ARCHITECTURE.md), [REST usage](docs/api.md),
[worker operations](docs/worker.md) and [trade-offs](DECISIONS.md).

DOCX translates body paragraphs and paragraphs in top-level table cells in reading
order, including merged cells, while preserving table structure and paragraph
styles. Nested tables, headers and footers remain unchanged; inline formatting
in translated paragraphs is replaced by a plain run. Existing persisted DOCX
extractions remain renderable but do not gain table blocks retroactively;
byte-identical re-uploads reuse those extractions.

## Architecture and acceptance criteria

```text
Browser ───────► web (FastAPI) ─┐
Claude / Cursor ► mcp (FastMCP) ─┼──► shared core services
                    worker ─────┘    claims, translates, renders
                                     │
                 shared /data volume: SQLite WAL, uploads, outputs
```

`web` and `mcp` are thin doors onto the same core services. The separate worker
executes queued translations. All three processes share `/data` for durable job
state and document files.

This is for product and operations teams that need reliable translations of
business documents with predictable operations. It is not aimed at professional
linguists who need computer-assisted translation (CAT) tools and workflows.

| Acceptance criterion | Implementation and known limit |
| --- | --- |
| Resilience | SQLite WAL and chunk leases let a restarted worker resume from committed work after `kill -9`. A committed translation is not repeated; a provider timeout with an unknown outcome can still cause a duplicate invocation and unreported billing. |
| Cost discipline | The semantic block cache reuses translations for matching inputs, so a repeat bulk run can avoid provider calls. `cache_hits_total` and `cache_misses_total` sum durable job lookup counts; document triage spend is persisted separately from job spend; billing from ambiguous invocations with unknown usage remains excluded. |
| Multi-language | One submission creates independently tracked jobs per target language, with separate progress and cost. A failure in one language does not stop the others. |

| Hard requirement | Where it lives |
| --- | --- |
| Upload a PDF through the web app and download a translated PDF | `frontend/`, `app/api/`, document/job services, and the PDF extractor/renderer |
| Real OpenAI API behind a provider interface | `app/adapters/llm/openai_provider.py`; tests use the fake provider |
| OpenAI Agents SDK with tool calling | `app/adapters/llm/triage_agent.py` and its navigation tools |
| MCP usable from Claude Code and Cursor | `app/mcp_server/` and the editor setup below |
| At least two document formats | PDF, DOCX and Markdown adapters in `app/adapters/formats/` |
| Resume translation after `kill -9` | Worker leases and persisted chunk checkpoints in `app/worker/` and persistence adapters; exercised by `scripts/chaos-restart.sh` |
| Fresh-clone Docker Compose deployment | `Dockerfile`, `docker-compose.yml`, and the CI image build |
| Required submission documents | `README.md`, `PROMPTS.md`, and `DECISIONS.md` |

### Where an agent earns its keep

Triage limits are configurable with `TRIAGE_MAX_TURNS` (default 8, range 1–20)
and `TRIAGE_TIMEOUT_SECONDS` (default 60, finite >0, at most 300 seconds).
Non-retryable provider errors and exhausted turn budgets immediately publish
a degraded plan; other failures retain up to three attempts. The service guard
is five seconds longer than the adapter timeout. MCP polling remains independent.
Batch pages show cumulative document analysis cost once alongside per-job bulk
translation costs; these estimate reported usage, not complete provider billing.

The OpenAI Agents SDK is used for triage only. Triage must inspect an unknown
document whose text can exceed one context window, navigate selected text with
tools, and make a judgment about domain, register, and terminology that shapes
every downstream chunk. Bulk translation maps a fixed prompt over independent
chunks; it deliberately avoids an agent loop to keep work deterministic,
parallel, and predictable in cost. See [ARCHITECTURE.md](ARCHITECTURE.md) for
the full design.

## Compose quickstart

From a clean clone with Docker and Compose installed:

```bash
docker compose up --build -d --wait
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/readyz
```

Open `http://localhost:8000` to upload a PDF, DOCX or Markdown; MCP listens on
`http://localhost:8001/mcp`. The stack defaults to `LLM_PROVIDER=fake` and
requires no API key. Three non-root processes share one named `/data` volume;
only the dedicated `MCP_HOST_SHARED_DIR` is bind-mounted for editor files.
`docker compose down` keeps documents and results; deleting the volume deletes
those records. `make up`, `make down`, and `make logs` wrap Compose commands.

For live translation copy `.env.example` to `.env`, configure the key, set
`LLM_PROVIDER=openai`, and recreate the services with `docker compose up -d`.
Keep `.env` local. The fixed upload limits are enforced by the services, rather
than configurable `MAX_FILE_SIZE_MB` or `MAX_PAGES` environment variables.

The offline crash check in the verification table creates an isolated Compose
project, kills the worker while chunks remain, then compares durable SQLite
checkpoints after restart. See [operator notes](docs/ops.md) for `--keep`,
counters and the at-least-once boundary for interrupted provider requests.

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

Re-uploading an edited document reuses unchanged text when the analysis plan and other cache inputs match; the UI shows the cached share of block lookups.

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

Upload a PDF, DOCX or Markdown and select target languages. The UI waits for analysis by
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

## Verification commands and prerequisites

The automated suite never calls the real provider. Live tests are opt-in because
they call OpenAI and can incur charges. Locally, application settings read the
process environment; they do not automatically load `.env`. Compose loads
`.env`, and `measure_quality` can load a file when passed `--env-file`.

| Command | What it covers | Prerequisites |
| --- | --- | --- |
| `make test` | Offline backend suite, using fake providers | Python dependencies from `uv sync`; no key, Docker, or running services |
| `make test-live` | Opt-in tests against the real OpenAI provider | Exported OpenAI key, network access, and possible API charges; no Docker or running services |
| `make lint` | Ruff checks and formatting | Python dependencies; no key, Docker, or running services |
| `make typecheck` | Backend mypy checks | Python dependencies; no key, Docker, or running services |
| `npm --prefix frontend test` | Frontend tests | Node.js and frontend dependencies installed with `npm --prefix frontend ci`; no key, Docker, or running services |
| `npm --prefix frontend run typecheck` | Frontend TypeScript checks | Node.js and frontend dependencies installed; no key, Docker, or running services |
| `npm --prefix frontend run build` | Production frontend bundle | Node.js and frontend dependencies installed; no key, Docker, or running services |
| `uv run pre-commit run --all-files` | Ruff hooks and gitleaks secret scan | Python dependencies from `uv sync`; hooks may need network access on first run; no OpenAI key, Docker, or running services |
| `make up` | Build and start the Compose stack | Docker Engine and Compose; services are started and remain running; fake provider is the default |
| `docker compose up --build -d --wait` | Fresh build, startup, and container health checks | Docker Engine and Compose; starts services; no key with the default fake provider |
| `./scripts/chaos-restart.sh` | Isolated fake-provider Compose run that kills and restarts a worker, then checks durable recovery | Docker Engine, Compose, and available local ports; builds/starts its own services; no key |
| `docker compose config --quiet` | Compose configuration validation without printing resolved settings or starting containers | Docker Compose CLI; no Docker daemon, key, or running services |
| `docker build .` | Build the production image | Docker Engine; may need network access to fetch base images/dependencies; no key or running services |
| `uv run python -m scripts.measure_quality samples/sample_en.pdf --env-file .env` | Live-only quality and cost measurement through the real pipeline; optionally add `--reference PATH`, `--models gpt-4o-mini,gpt-4o`, or `--target-language fr` | `LLM_PROVIDER=openai`, OpenAI key in the selected environment file, network access, and API charges; no Docker or running services |
| `git status --short` | Check for remaining working-tree changes | Git repository; no key, Docker, or running services |
| `git log --oneline -5` | Inspect recent delivery history | Git repository; no key, Docker, or running services |
| `curl -fsS http://localhost:8000/healthz` and `curl -fsS http://localhost:8000/readyz` | Check Compose web liveness and readiness | Web service running on port 8000; no OpenAI key with the fake provider |

The `--env-file` option above is specific to `measure_quality`; other local
commands need settings exported in their process environment. For example,
copy `.env.example` to `.env` for Compose, or export the needed values before
running local services and `make test-live`.

Offline backend tests use fake providers, real temporary WAL databases,
FastMCP's in-memory client and worker-produced PDF/DOCX artifacts. The explicit
measurement command rejects a fake provider and reports JSON plus a readable
table. Without `--reference`, chrF is labeled as back-translation, a coarse
information-preservation proxy. Persisted bulk attempt cost excludes triage; document-level triage is recorded
separately with cached-aware pricing. Unknown transport usage remains excluded; one job duration cannot establish a population p95 or
a parallelism comparison. Measurements and gaps are recorded in
[DECISIONS.md](DECISIONS.md).

For the licensed reference sample, convert its English lines to a temporary
DOCX and compare models through the same command:

```bash
uv run python - <<'PY'
from pathlib import Path
from docx import Document
sample = Document()
for line in Path("samples/golden_en.txt").read_text(encoding="utf-8").splitlines():
    sample.add_paragraph(line)
sample.save("/tmp/golden_en.docx")
PY
uv run python -m scripts.measure_quality /tmp/golden_en.docx --reference samples/golden_de_ref.txt --models gpt-4o-mini,gpt-4o --env-file .env
```

Each model gets a fresh temporary database and cache. JSON goes to stdout;
Markdown goes to stderr and labels the quality mode. Matrix cost includes
known bulk and triage usage estimates; request counts cover recorded bulk
attempts only. Corpus provenance, licence and synthetic literal adaptations
are documented in [golden_dataset.md](samples/golden_dataset.md).

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
| Cost rises | `/metrics` exposes bulk `llm_cost_usd_total` and document `llm_triage_cost_usd_total`; query attempts and analyses below | Check retries and `MAX_COST_PER_JOB_USD`; reduce concurrency if rate limited, correct provider failures, and stop accepting new work while investigating. Recorded spend excludes unknown or uncheckpointed usage. Triage expense is shared once per document across all languages. |
| Partial results | Job status `completed_with_errors` means missing translations rendered as source text; inspect `GET /api/jobs/{id}` errors | Resolve the reported cause; `curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' http://localhost:8000/api/jobs/<job-id>/retry` requeues only missing work. Download again after completion. |
| Triage remains analyzing | `GET /api/documents/{id}` and web/MCP logs; analysis tasks run in-process | After a crashed owner, `curl -fsS -X POST http://localhost:8000/api/documents/<document-id>/retry-triage`; successful analysis and analysis already used by jobs remain immutable. |

Read durable diagnostics without changing state:

```bash
docker compose exec -T web sqlite3 /data/app.db "SELECT job_id,seq,status,lease_expires_at FROM chunks ORDER BY job_id,seq;"
docker compose exec -T web sqlite3 /data/app.db "SELECT outcome,COUNT(*),SUM(cost_usd) FROM chunk_attempts GROUP BY outcome;"
docker compose exec -T web sqlite3 /data/app.db "SELECT SUM(cost_usd),SUM(CASE WHEN attempt_no>1 THEN cost_usd ELSE 0 END) FROM chunk_attempts;"
docker compose exec -T web sqlite3 /data/app.db "SELECT document_id,cost_usd,cost_usd_total,tokens_in_total,tokens_out_total FROM document_analyses;"
```

`/healthz` is dependency-free web liveness. `/readyz` checks the database,
writable storage, and stale `inflight` chunk leases: 503 appears when lease
expiry is more than `max(120, 2 * CHUNK_LEASE_SECONDS)` seconds in the past.
An idle deployment reports ready even if the worker is absent; a paused worker
can temporarily report not ready until it recovers. Worker container health
checks process liveness; MCP health checks its transport. Neither supplies a
separate HTTP `/healthz` endpoint.

Metrics expose durable job counts, bulk cost/error totals, and separate triage
cost/token totals. `llm_triage_tokens_total` uses `direction="input"` and
`direction="output"`. Current-plan analysis columns are overwritten on retry;
`_total` columns accumulate known usage from every attempt. Scrapes and restarts
do not recount it. Existing database rows migrate with zeros; triage spend before
this instrumentation is permanently unrecorded.
`cache_hits_total` and `cache_misses_total` sum durable per-job lookup counts; latency distributions
and retry spend are obtained from SQLite by the measurement command. Unknown
usage from a killed or timed-out invocation cannot be reconstructed from a
missing attempt row. See [operator notes](docs/ops.md) and
[worker operations](docs/worker.md).

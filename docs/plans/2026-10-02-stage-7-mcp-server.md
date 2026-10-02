# Stage 7 — MCP Server Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a FastMCP streamable-HTTP front door exposing the approved document-translation workflow tools, sharing the existing core services and adapters with REST rather than calling the local FastAPI server over HTTP.

**Architecture:**
- **Direct service calls.** MCP tools compose and call `DocumentService`, `JobService`, `TriageService`, repository ports, and `FileStorage` directly. Do not create an `httpx` client or call this application's FastAPI endpoints from MCP.
- **Shared core behavior.** MCP is a thin protocol adapter. Chunking, idempotency, retries, status decisions, and persistence remain in existing core services and persistence adapters. MCP code must not contain SQL.
- **Connection lifecycle.** Repository instances receive active SQLite connections. Tool operations create/close connections through `SqliteConnectionFactory`; do not keep a connection or transaction open while waiting for triage or other network I/O. Status polling uses short-lived connections.
- **Triage readiness.** After upload, MCP starts triage using the existing `TriageService` and agent implementation. It polls `DocumentRepository` until status becomes `extracted` or `failed`; only then does it call `JobService.create_jobs`.
- **File access boundary.** MCP can read input files and write downloads only under a dedicated host directory mounted into the server container. Every input/output path must be resolved and checked for containment, including symlinks.
- **Recent jobs.** Add `JobService.list_recent_jobs(limit)` and expose it through `GET /api/jobs?limit=10`; MCP calls the service directly. The exact collection route is `/api/jobs`, with `limit` as a query parameter.
- **Transport.** FastMCP streamable HTTP, bound to `0.0.0.0:8001`, path `/mcp`.

**Tech Stack:** Python 3.12, FastMCP, FastAPI, aiosqlite, Pydantic v2.

**Current State:**
- `app/mcp_server/` does not exist.
- `app/core/services/` provides `DocumentService`, `JobService`, and `TriageService`.
- FastAPI already composes the services, repositories, format adapters, storage, and triage agent.
- `JobService` has no `list_recent_jobs` method; persistence currently exposes no recent-jobs query.
- There is no Docker Compose file yet; full container delivery remains part of Stage 9.

---

## Task 1: Add MCP shared-directory settings and path validation

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`
- Create: `app/mcp_server/paths.py`
- Create: `tests/mcp_server/test_paths.py`

Add `mcp_shared_dir: Path = Path("/mcp-files")` (the path inside the container) and bounded settings for triage polling (poll interval and deadline). For Compose, use a separate host-side `MCP_HOST_SHARED_DIR` variable as the bind-mount source; do not use one variable for both host and container paths.

Implement one path resolver for input and output paths:

```python
def resolve_shared_path(root: Path, supplied_path: str, *, must_exist: bool) -> Path:
    resolved_root = root.resolve(strict=True)
    candidate = Path(supplied_path)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("path is outside the configured MCP shared directory")
    return resolved
```

Also require input paths to be regular files and output directories to exist or be safely created under the shared root. Resolve symlinks before containment checks. Never accept arbitrary paths outside the mount.

**Tests:** normal input/output paths succeed; `../` traversal and symlink escapes fail; missing files and non-file inputs fail safely.

---

## Task 2: Add recent-job query to the core service and REST API

**Files:**
- Modify: `app/core/services/job_service.py`
- Modify: `app/adapters/persistence/api.py`
- Modify: `app/api/routers/jobs.py`
- Modify: `tests/adapters/persistence/` recent-job tests
- Modify: `tests/api/` job-route tests

Add `JobService.list_recent_jobs(limit: int) -> list[JobRecord]`.

- Validate/clamp `limit` to the agreed bounded range (default 10, maximum 100).
- Add the corresponding query to the persistence adapter, ordered newest-first by `created_at`.
- Expose it as `GET /api/jobs?limit=10` returning `list[JobSummaryResponse]`.
- Keep the route a thin delegate to `JobService`; do not add SQL to the router.

**Tests:** service ordering and limit bounds; API response shape; empty result returns `[]`.

---

## Task 3: Create MCP service composition and server entrypoint

**Files:**
- Create: `app/mcp_server/__init__.py`
- Create: `app/mcp_server/runtime.py`
- Create: `app/mcp_server/server.py`

Build a composition layer for MCP that creates service instances from the same adapters and ports as FastAPI:

- `SqliteConnectionFactory`
- `SqliteDocumentRepository`, `ApiJobExecutionRepository`, `SqliteTranslationCacheRepository`
- `ApiPersistence`
- `DocumentService`, `JobService`, `TriageService`
- `FilesystemStorage`, `FormatRegistry`, `ModelCostCalculator`
- configured `FakeTriageAgent` or `OpenAITriageAgent`

FastMCP owns the server lifecycle. Use the installed FastMCP API (`FastMCP`, `@mcp.tool`, and `run(transport="streamable-http", host=..., port=..., path=...)`). Close providers and connections deterministically. Do not import `app.api` routers or dependencies into `app.mcp_server`; share services and adapters, not web-controller code.

**Tests:** server exposes the approved tool names and starts with test settings.

---

## Task 4: Implement `translate_file`

**Files:**
- Modify: `app/mcp_server/server.py`
- Modify: `app/mcp_server/runtime.py`
- Create: `tests/mcp_server/test_translate_file.py`

Tool contract:

```python
translate_file(path: str, target_languages: list[str]) -> TranslationSubmission
```

Flow:
1. Resolve `path` under `mcp_shared_dir`; reject traversal, symlink escapes, unsupported formats, and uploads over the existing document size cap.
2. Read the file without blocking the event loop (`asyncio.to_thread` for filesystem reads).
3. Call `DocumentService.upload(filename, content)` using a short-lived connection. Duplicate content reuses the existing document ID per the service contract.
4. If document status is `analyzing`, start triage using a dedicated task and its own connection through the shared service composition. Do not import the FastAPI `BackgroundTasks` helper.
5. Poll `DocumentRepository.get_document(document_id)` until status is `extracted` or `failed`. Each poll uses a short-lived connection; sleep asynchronously between polls. Stop at the configured deadline and return a retryable, safe MCP error containing the document ID. If `failed`, return the catalogued safe error.
6. Once status is `extracted`, call `JobService.create_jobs(document_id, target_languages, idempotency_key)`.
7. Return document ID, batch ID, and job IDs immediately after enqueue; do not wait for translation completion.

If an existing document remains `analyzing` (for example, a previous MCP process was killed), invoke the same triage service flow again before polling. Existing successful analysis must not be overwritten.

**Tests:** fake triage transitions the document to `extracted`; then jobs are created. Also test already-extracted documents, failed triage fallback, deadline behavior, unsupported files, and path escape rejection.

---

## Task 5: Implement status, download, and recent-job tools

**Files:**
- Modify: `app/mcp_server/server.py`
- Create: `app/mcp_server/schemas.py`
- Create: `tests/mcp_server/test_job_tools.py`

Tools:

```python
check_status(job_id: str) -> JobStatusResult
download_result(job_id: str, output_dir: str) -> DownloadResult
list_recent_jobs(limit: int = 10) -> list[JobSummary]
```

- `check_status`: delegate to `JobService.get_job`; return progress, cost, status, and safe error fields.
- `download_result`: require a terminal downloadable job; obtain the artifact through `FileStorage`, copy it to a validated destination under `mcp_shared_dir`, and return the path inside the mount. File copying runs via `asyncio.to_thread`.
- `list_recent_jobs`: delegate to `JobService.list_recent_jobs`; do not query SQLite from the MCP tool.
- Use explicit Pydantic result models in `app/mcp_server/schemas.py` so tool results have stable structured shapes.

**Tests:** missing job, non-terminal download, successful copy, output path traversal/symlink rejection, list limit, and empty list.

---

## Task 6: Register streamable-HTTP server and shared mount

**Files:**
- Create: `app/mcp_server/__main__.py`
- Modify: `.env.example`
- Create or modify: `docker-compose.yml` when container wiring is introduced

Run MCP on `0.0.0.0:8001/mcp`. Mount a dedicated host directory (Compose source `MCP_HOST_SHARED_DIR`, default `./mcp-files`) into the MCP container at `/mcp-files` (`MCP_SHARED_DIR` setting). Document `input/` and `output/` paths. Do not mount the whole host filesystem.

If Stage 9 owns the compose stack, Stage 7 must still verify the server locally using `python -m app.mcp_server`; add the MCP compose service during the later delivery stage without changing the tool contracts.

---

## Task 7: MCP contract and end-to-end tests

**Files:**
- Create: `tests/mcp_server/test_server.py`
- Create: `tests/mcp_server/test_tools.py`

Use FastMCP's in-memory `Client(server)` for protocol-level tests with fake dependencies. Add an end-to-end test that places a sample PDF in the temporary shared input directory, invokes `translate_file`, confirms returned job IDs, then checks status and downloads after the worker has completed the job.

No live LLM calls are required; use the fake provider and fake triage agent.

---

## Task 8: README and manual verification

**Files:**
- Create or modify: `README.md`

Document:
- Exact Claude Code configuration for `http://localhost:8001/mcp`.
- How to configure the dedicated shared directory and where input/output files go.
- Three-step verification: place a sample in the input mount, call `translate_file`, poll with `check_status`, download with `download_result`.
- Explain that the MCP server can access only the configured mount, not arbitrary host paths.

Manually connect with a clean Claude Code / Cursor configuration and translate a sample document without using the web UI.

---

## Task 9: Verification and task tracking

```bash
make test
make lint
make typecheck
```

Create sequential Stage 7 task IDs in `TASKS.md` before implementation, mark them in progress, and backfill commit hashes on completion. Proposed decomposition:
- `DT-41`: shared directory config and path containment.
- `DT-42`: recent-jobs service/persistence query and `GET /api/jobs?limit`.
- `DT-43`: MCP composition and FastMCP runtime.
- `DT-44`: translate-file workflow with triage polling.
- `DT-45`: status/download/recent tools and protocol/E2E tests.
- `DT-46`: README, manual verification, and delivery review.

Expected exit: `make test`, `make lint`, and `make typecheck` pass; MCP can complete document translation through the shared core services without calling FastAPI over HTTP.

---

## Critical Agent Reminders

1. **No HTTP client to the same application.** Do not use `httpx` to call FastAPI. MCP tools call core services and ports directly.
2. **No direct SQL in MCP.** Use repositories through `DocumentService` / `JobService` / `TriageService`.
3. **Shared-directory sandbox.** Resolve and validate every supplied input/output path against the configured mount, including symlinks. Never allow arbitrary filesystem access.
4. **Triage polling is local.** Poll `DocumentRepository` (or a service built over it) until `EXTRACTED`/`FAILED`; do not poll REST endpoints.
5. **Short DB connection scopes.** Open/close active connections around service/repository operations; never hold a transaction during triage's network call or polling sleep.
6. **Async I/O.** Use `asyncio.to_thread` for local filesystem reads/copies; use `asyncio.sleep` during status polling.
7. **Shared behavior.** Keep chunking, idempotency, analysis gating, and status transitions inside core services.

## Approved execution refinements and results

The user approved this plan with atomic service-level triage claiming before
background scheduling and a polling deadline no greater than 45 seconds.
Implementation DT-41–DT-46 is recorded in
[Stage 7 execution](2026-10-02-stage-7-mcp-server-execution.md), including
delegation, concurrency/lifecycle corrections and verification evidence.
Full offline acceptance: 421 tests pass; lint and typecheck clean. Real local
HTTP completes sample translation and download with a fake worker. Manual
clean-config Claude Code returned an error; authenticated editor validation
remains unverified. Compose wiring remains scheduled for Stage 9.

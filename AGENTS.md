# AGENTS.md — Project Guardrails

You are working on **Document Translator** — an async document translation
service (PDF/DOCX → LLM → same format out). `ARCHITECTURE.md` in this repo
is the single source of truth: **read it before any code change** and
reference sections by title, not number (numbers drift).

## Stack (pinned — do not improvise)

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async, aiosqlite) · Pydantic v2 ·
FastMCP · PyMuPDF · python-docx · `openai` + `openai-agents` · tiktoken ·
structlog · uv. Exact versions live in `uv.lock`. Add dependencies ONLY via
`uv add` / `uv add --group dev`; never hand-edit pins, never `pip install`.

## Rules

1. **Database.** Every SQLite connection: `PRAGMA journal_mode=WAL`,
   `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=20000`
   (milliseconds = 20 s). Application startup explicitly initializes WAL
   before accepting work. Services initiate writes. The aggregate
   `create_job_with_chunks` repository operation owns one atomic transaction
   and rolls back the job and all chunks on failure; other writes happen
   inside service-layer transactions. SQL lives exclusively in
   `app/adapters/persistence/`.
2. **Opaque Metadata.** The core pipeline handles only `seq` +
   `source_text`. Never parse, inspect, or transform `format_metadata`
   outside the format adapter that owns it (`adapters/formats/pdf.py`,
   `docx.py`).
3. **Layering.** `app/core/` imports NOTHING from `api/`, `mcp_server/`,
   `worker/`, or `adapters/`. `api/` and `mcp_server/` are thin doors over
   core services — no business logic, no direct DB access.
4. **Contracts first (human-in-the-loop).** New public contracts — REST
   endpoints, domain Pydantic models, DB schema, MCP tools — are proposed as
   drafts and implemented ONLY after explicit user approval. Internal
   implementation details are free.
5. **LLM boundary.** All provider calls go through
   `core/ports.py::LLMProvider`. Tests use `FakeProvider` exclusively; real
   OpenAI calls only under `@pytest.mark.live` (opt-in: `make test-live`).
6. **Errors.** Outward errors use codes from `app/core/errors.py`:
   `{error_code, message, retryable}`. Never leak raw exceptions or
   tracebacks to clients. Extend the catalog; don't invent ad-hoc strings.
7. **Secrets.** Never print, log, or commit environment variable values.
   Config is accessed only via `app/config.py`. `.env` is gitignored;
   `.env.example` holds placeholders only.
8. **No-hallucination policy.** Unsure about a third-party API
   (openai-agents, PyMuPDF, FastMCP…)? Read the installed source in
   `.venv/` FIRST, then official docs via web search. Do not invent
   methods, parameters, or behavior.
9. **Async discipline.** Async all the way on I/O paths. No blocking calls
   (`requests`, `time.sleep`, sync `sqlite3`) inside async code. CPU-bound
   work (PDF rendering) goes through `asyncio.to_thread`.
10. **Git & task log.** Commit messages follow `CONTRIBUTING.md` exactly:
    `DT-<N>: <type>(<scope>): <short description>`. Ticket IDs come from
    `TASKS.md` — the repo-local backlog you maintain: before starting work,
    decompose the current stage into numbered tasks or pick the next
    `todo`; mark it `in-progress`; on completion mark it `done` and
    backfill commit hashes. Never commit unless explicitly asked. Never
    amend, rebase, or force-push — those are human PR-preparation
    activities (CONTRIBUTING.md, Clean History Rules).

## Commands

`make setup` · `make test` · `make test-live` · `make lint` ·
`make typecheck` · `make format` · `make dev` · `make up` / `make down`

## Definition of done

`make test` green · `make lint` clean · `make typecheck` clean · error
catalog used · docs updated when behavior changes.

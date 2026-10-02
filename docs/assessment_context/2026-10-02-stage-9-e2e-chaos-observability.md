# Stage 9 — End-to-End, Chaos, Observability, Measurements

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver containerized operation, a reproducible restart-under-chaos test, worker-aware readiness, honest quality/cost measurements, and an operator runbook — so a reviewer can clone, build, restart mid-translation, and trust the reported numbers.

**Architecture:**
- **Multi-stage image.** A Node stage builds the frontend bundle; the Python runtime stage contains only the application and its runtime dependencies. Three processes (`web`, `worker`, `mcp`) share one `/data` volume.
- **Chaos proof from durable state.** The chaos script asserts exactly-once behaviour using the SQLite tables we already designed for observability (`chunk_attempts`, `block_translations`, `chunks`). No new invocation-logging machinery is added to `FakeProvider`.
- **Stateless worker-liveness heuristic.** `/readyz` reports 503 when the database shows chunks still `inflight` whose `lease_expires_at` is more than the grace period in the past. No heartbeat table and no marker files.
- **Live measurements for reported numbers.** Final cost, latency, and quality figures in `DECISIONS.md` come from one real OpenAI run of the sample document. CI and the automated suite keep using `FakeProvider`.
- **Honest boundaries.** Reported numbers state what was measured, on which document, and which guarantee boundary applies (exactly-once for committed results, at-least-once for provider invocations).

**Tech Stack:** Docker/Compose, bash + `sqlite3` CLI, Python 3.12, `uv`.

**Current State:**
- No `Dockerfile` and no `docker-compose.yml` exist; README explicitly defers container delivery to this stage.
- `scripts/` contains only `generate_sample_docs.py`.
- `/healthz`, `/readyz`, `/metrics` exist; `/readyz` currently checks only DB and storage writability.
- `chunk_attempts` (per-invocation tokens/cost/latency/outcome) and `block_translations` (unique per `translation_key`+`block_id`) already exist.
- `DECISIONS.md` has Stage 3 format measurements but no cost/latency/quality numbers.

---

## Task 1: Multi-Stage Dockerfile

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

Stages:
1. **Frontend build** — Node 24, `npm ci`, `npm run build`, output `frontend/dist`.
2. **Python runtime** — Python 3.12 on a slim base, `uv sync --frozen` for runtime dependencies only, application code, and `frontend/dist` copied from stage 1.

Requirements:
- Run as a non-root user.
- Install `curl` for healthchecks and **`sqlite3` for the chaos script** — `python:*-slim` does not ship the `sqlite3` CLI, and the approved chaos verification depends on it.
  **Approved compromise:** shipping the `sqlite3` client in the production image is accepted for this assessment. Verification speed matters more than a minimal image surface here; the package adds no meaningful attack surface and no runtime dependency to the application. Do not work around its absence with Python one-liners or by parsing logs.
- Provide `HEALTHCHECK` and an entrypoint that can act as `web`, `worker`, or `mcp`.
- Exclude `.venv`, caches, `frontend/node_modules`, `.git`, and local databases via `.dockerignore`.

**Exit criteria:** `docker build` succeeds; `docker run --rm <image> python -c "import app"` and `sqlite3 --version` both succeed.

---

## Task 2: Compose Stack for Three Processes

**Files:**
- Create: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`

Services:
- `web` — FastAPI, serves REST and the frontend build, `ports: 8000:8000`.
- `worker` — `python -m app.worker`.
- `mcp` — `python -m app.mcp_server`, `ports: 8001:8001`.

Shared configuration:
- One named volume mounted at `/data` for `app.db`, `uploads/`, and `out/`.
- `MCP_HOST_SHARED_DIR` → bind-mounted at `/mcp-files` (`MCP_SHARED_DIR`). Mount only that directory; never the whole host filesystem.
- Worker-referenced variables (`DATABASE_PATH`, `UPLOAD_STORAGE_PATH`, `OUTPUT_STORAGE_PATH`) must match the web service so all three processes open the same database.

Reliability:
- Healthchecks against `/healthz`; `depends_on` conditioned on health.
- `restart: unless-stopped` for `worker` so a killed worker is replaced.
- `LLM_PROVIDER=fake` documented as the offline default for compose verification; live runs use the real provider.

**Exit criteria:** `docker compose up --build` from a clean clone reaches healthy state for all three services.

---

## Task 3: Restart-Under-Chaos Verification

**Files:**
- Create: `scripts/chaos-restart.sh`
- Create: `docs/ops.md` (or extend `docs/worker.md`)

The script reproduces the reviewer's test: start a translation, `kill -9` the worker mid-flight, restart it, and prove recovery plus the exactly-once boundary using durable state via the `sqlite3` CLI.

Sequence:
1. Bring up the stack offline (`LLM_PROVIDER=fake`) with a sample document large enough to have several chunks.
2. Enqueue a job; wait until at least one chunk reaches `done` and at least one is `pending`.
3. Snapshot counters **before** the kill:
   ```bash
   sqlite3 "$DB" "SELECT COUNT(*) FROM block_translations;"
   sqlite3 "$DB" "SELECT id, seq FROM chunks WHERE status='done' ORDER BY seq;"
   sqlite3 "$DB" "SELECT chunk_id, COUNT(*) FROM chunk_attempts GROUP BY chunk_id;"
   ```
4. `docker compose kill -s KILL worker`; wait for the lease grace period.
5. Restart the worker; wait for a terminal job status.
6. Snapshot counters **after** completion and assert the durable invariants.

Assertions — these are the claims that the database can actually prove:
- **No duplicate committed translations.** `block_translations` has at most one row per (`translation_key`, `block_id`); final count equals the number of distinct blocks for that translation key.
- **Committed work is never re-requested.** Every chunk that was already `done` before the kill still has exactly the attempt rows it had before the kill (`attempt_no` did not advance for it). Newly created attempt rows must belong only to the chunk that was interrupted while `inflight`.
- **Progress is monotonic.** `chunks` reaches `done`, the job reaches `done` or `completed_with_errors`, and the output artifact exists.

Documented expectation, stated honestly in the script output and runbook: `chunk_attempts` row count **may** grow after the restart, because a chunk interrupted while `inflight` has no committed result and must be re-executed. That is the documented at-least-once boundary for provider invocations, not a violation of exactly-once for committed results. The script must print both numbers and label them.

**This interpretation is correct and is the only defensible one.** It matches the guarantee boundary already stated in `ARCHITECTURE.md`: exactly-once applies to *committed business results* (`block_translations`), while provider *invocations* are at-least-once under ambiguity. A `kill -9` mid-request is precisely the ambiguous window — the process cannot know whether the provider processed the call — so a growing `attempt_no` for that one chunk is required behaviour, not a defect. Any reading in which attempts never increase after a kill would be claiming a guarantee no external API can provide.

The script exits non-zero when an assertion fails, and must not leave the stack running on failure unless `--keep` is passed.

**Exit criteria:** the script passes on a clean compose run and its output distinguishes committed-exactly-once from invocation-at-least-once.

---

## Task 4: Worker-Aware Readiness

**Files:**
- Modify: `app/core/services/health_service.py`
- Modify: `app/adapters/persistence/` health/metrics query
- Modify: `app/api/routers/health.py`
- Modify: `app/core/errors.py` (catalog message, if needed)
- Modify: `tests/api/test_health.py`

Implement the approved stateless heuristic in `/readyz`:

```sql
SELECT COUNT(*) FROM chunks
WHERE status = 'inflight'
  AND lease_expires_at IS NOT NULL
  AND lease_expires_at < :now_minus_grace;
```

If the count is greater than zero, return HTTP 503 with the catalogued `not_ready` envelope. The grace period must exceed `CHUNK_LEASE_SECONDS` (default 60 s); default it to 120 s and derive it from the lease setting where practical.

Requirements:
- The query lives in the persistence adapter; `HealthService` stays a thin decision point.
- No new tables, no marker files, no schema change.
- `/healthz` remains dependency-free and is unaffected.

Stated limitations to document rather than hide:
- An idle system with no `inflight` chunks reports ready; absence of evidence is not proof of a live worker.
- A chunk legitimately in flight past its lease (slow render, paused process) is reported as not ready until it recovers.

**Exit criteria:** tests prove 503 for a stale `inflight` chunk, 200 for a healthy/expired-to-`pending` state, and 200 when no chunks are in flight.

---

## Task 5: Quality and Cost Measurement Script

**Files:**
- Create: `scripts/measure_quality.py`
- Create: `scripts/__init__.py` only if imports require it

Behaviour:
- Accept a document path, target language (default `de`), and optional reference-translation path.
- Run the real pipeline (upload → triage → job → worker → render) and collect per-job cost, tokens, and latency from `jobs` and `chunk_attempts`.
- Compute **chrF**: reference-based when a reference is supplied, otherwise back-translation (source → target → source) and clearly label which mode produced the score.
- Compute **Number/Placeholder Preservation %** across numbers, dates, currency amounts, and placeholder patterns (`{{...}}`, `%s`, `[...]`).
- Emit machine-readable JSON and a human-readable table; both are suitable for pasting into `DECISIONS.md`.

Operational rules:
- Not part of `make test`; invoked explicitly.
- Requires live credentials for the reported figures and must fail loudly if `LLM_PROVIDER` is `fake`, instead of silently reporting fabricated numbers.
  **Enforced:** this is a hard requirement, not a preference. The script must exit non-zero with an explicit message when run against a fake provider, so an offline run can never be mistaken for a measurement.
- Repeatable: fixed sample document, deterministic seed where applicable.

**Exit criteria:** running it with a reference produces both metrics plus cost/latency; running it without a reference labels the back-translation mode.

---

## Task 6: Live Measurement Run and `DECISIONS.md` Completion

**Files:**
- Modify: `DECISIONS.md` (measured-numbers section)

Perform one real OpenAI run of the sample document and record:
- **Cost per document**, with the retry share and what dominates it.
- **p95 chunk latency** and **p95 job latency**, before/after chunk parallelism where measurable.
- **chrF** (with the mode stated).
- **Number/Placeholder Preservation %**.

Rules:
- Use `LLM_PROVIDER=openai` with a real key; never present `FakeProvider` output as a cost or quality measurement.
- Each number must name the document, chunk/block counts, model, and date.
- If a figure could not be measured, write "not measured" and say why — a stated gap reads as judgment; an invented number destroys trust.
- Remove the placeholder text currently occupying the measured-cost section.
- **No invented numbers under any circumstance.** If the live run cannot be completed (missing key, provider outage, insufficient budget), the section states that plainly instead of carrying illustrative values.

**Exit criteria:** the section contains only real, reproducible measurements or explicit "not measured" statements.

---

## Task 7: Operator Runbook and CI

**Files:**
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml`

README "3 a.m." runbook — symptom → metric/command → safe action:
- Jobs stuck queued → worker liveness, `/readyz`, shared database settings.
- Jobs stuck running → lease recovery timing and `chunks` state.
- Rising cost → `/metrics` totals and `chunk_attempts` by outcome.
- Partial results → `completed_with_errors` meaning, retry endpoint, cache-driven status.
- Triage stuck in `analyzing` → explicit retry endpoint; background tasks are in-process.

CI:
- Keep offline gates: `ruff`, `mypy`, `pytest -m "not live"`, and frontend typecheck/test/build.
- Never add live-provider calls to CI.

**Exit criteria:** the runbook resolves each listed symptom with a command the reviewer can run; CI stays fully offline.

---

## Task 8: Final Verification

```bash
make test
make lint
make typecheck
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
docker compose up --build
./scripts/chaos-restart.sh
```

Manual acceptance: from a clean clone, upload a PDF, get a translated PDF, restart mid-translation, confirm resume, confirm MCP translation without the web UI, and confirm `/metrics`, `/healthz`, `/readyz` behave as documented.

**Exit criteria:** every command above succeeds on a clean clone; the chaos script passes; reported numbers exist in `DECISIONS.md`.

---

## Task 9: Backlog and Delivery

Record Stage 9 task IDs in `TASKS.md` before implementation, mark them in progress, and backfill commit hashes on completion. Do not commit until explicitly requested.

---

## Critical Agent Reminders

1. **No invocation-logging in `FakeProvider`.** A `FAKE_INVOCATION_LOG` JSONL
   writer is explicitly rejected. Prove chaos outcomes with the `sqlite3` CLI
   against `chunk_attempts`, `block_translations`, and `chunks`.
2. **State the ambiguity honestly.** Interrupted `inflight` chunks are re-executed by design; attempt rows may grow. Exactly-once applies to committed translations.
3. **No heartbeat table or marker files.** `/readyz` uses the stale-`inflight`-lease query only.
4. **Install the `sqlite3` CLI in the image.** `python:*-slim` does not include it, and the approved chaos verification depends on it.
5. **Live numbers only in the report.** `DECISIONS.md` must contain real measurements or explicit "not measured"; never fake-provider figures.
6. **Grace period exceeds the lease.** Default 120 s against a 60 s chunk lease, derived from settings where possible.
# Stage 4 — Worker: Claim Loop, Leases, Executor

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the worker process that claims jobs, executes chunks with bounded parallelism, retries retryable failures, checkpoints translations, and assembles the final document.

**Architecture:**
- **Single-job concurrency.** The worker claims and processes exactly one job at a time. Bounded parallelism (semaphore 8) applies only to chunks within the current job. This prevents OOM and keeps heartbeat/lease logic simple.
- **Cache-driven assembly.** The worker never keeps `failed_block_ids` in memory. After all chunks finish, assembly reads every block translation for the job's `translation_key` from the cache. Blocks without a cached translation are rendered as source text; if any are missing, the job finishes as `COMPLETED_WITH_ERRORS`. This makes the design resilient to `kill -9`: the cache is the single source of truth for what is complete.
- **No DB locks during network calls.** The SQLite transaction (`BEGIN ... COMMIT`) must never span an `LLMProvider.translate_chunk` call. The provider call happens outside any DB transaction; only the result (cache inserts, attempt records, chunk completion) is persisted afterwards.
- **Soft cost cap with in-memory lock.** An `asyncio.Lock` protects the worker's local `cost_usd` accumulator while chunks under the semaphore check the cap and add estimated costs. The cap is a soft limit; a small overshoot is acceptable because exact synchronization would serialize all LLM calls.
- **Leases and heartbeats.** Job lease covers the whole execution; chunk leases cover in-flight chunks. A background task renews both on `heartbeat_interval_seconds`.
- **Retry policy.** Exponential backoff + jitter, up to `max_chunk_attempts` (default 4) for retryable provider/transport errors. Fatal errors exhaust the chunk immediately.
- **Graceful shutdown.** On `SIGTERM`/`SIGINT`, stop claiming new jobs, let in-flight chunks finish, then exit.

**Tech Stack:** Python 3.12, `asyncio`.

**Current State:**
- `app/worker/` does not exist.
- All required ports are defined in `app/core/ports.py`.
- `app/core/errors.py` already catalogues provider errors with retryability.

---

## Task 1: Worker Settings

**Files:**
- Modify: `app/config.py`

Add worker-specific settings:

```python
worker_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
job_lease_seconds: int = 60
chunk_lease_seconds: int = 60
heartbeat_interval_seconds: int = 10
max_chunk_concurrency: int = 8
max_chunk_attempts: int = 4
max_cost_per_job_usd: float = 2.0
```

**Exit criteria:** Settings load and validate.

---

## Task 2: Executor with Retry / Backoff / Jitter

**Files:**
- Create: `app/worker/executor.py`

Implement `Executor`:

```python
class Executor:
    def __init__(self, max_attempts: int) -> None: ...

    async def execute(self, task: Callable[[], Awaitable[T]]) -> T: ...
```

- Catch `ProviderError` and inspect `error.retryable`.
- Retryable errors: wait `min(base * 2 ** attempt + jitter, max_wait)` and retry.
- Fatal errors and exhaustion after `max_attempts`: re-raise the last error.

**Exit criteria:** Unit tests prove retry on retryable error, immediate fatal on non-retryable error, and exhaustion after max attempts.

---

## Task 3: Claim Loop

**Files:**
- Create: `app/worker/claim_loop.py`

Implement `ClaimLoop`:

```python
class ClaimLoop:
    def __init__(
        self,
        settings: Settings,
        job_repo: JobExecutionRepository,
        cache_repo: TranslationCacheRepository,
        document_repo: DocumentRepository,
        llm_provider: LLMProvider,
        cost_calculator: CostCalculator,
        format_registry: FormatRegistry,
        file_storage: FileStorage,
    ) -> None: ...
```

`run()`:
1. Loop while not shutting down.
2. `claim_job(worker_id, lease_expires_at)` — atomic `UPDATE ... RETURNING` that also recovers expired `running`/`assembling` jobs.
3. If no job, sleep 1 s and continue.
4. Set job status to `running`.
5. Start a background heartbeat task for the job.
6. Process the job:
   - Load document blocks.
   - For each pending chunk, claim it and submit to the executor under the semaphore.
   - Wait for all chunks to finish.
7. Stop the heartbeat task.
8. Call `Assembly.render(...)`.
9. On successful render, complete job `done` or `completed_with_errors` (determined by assembly based on cache).
10. On render failure, complete job `failed(render_failed)`.

`claim_chunks` uses atomic `UPDATE ... RETURNING` on `chunks` for `status='pending'` with expired/no lease.

**Exit criteria:** Unit tests prove the claim loop claims a job, processes chunks, and completes the job.

---

## Task 4: Translation Loop

**Files:**
- Create: `app/worker/translation_loop.py`

Implement `TranslationLoop.process_chunk(...)`:

1. Find the blocks belonging to this chunk (loaded by the worker from `chunk_blocks`).
2. Build `context_before` / `context_after` from the full document block list sorted by `seq`.
3. Compute the job's `translation_key`.
4. Check the cache for each block. Collect already-cached translations.
5. If **all** blocks are cached, skip the LLM call and mark the chunk done.
6. Before the LLM call:
   - Acquire the worker's in-memory `cost_lock`.
   - Estimate the provider call cost.
   - If `cost_usd + estimated_cost > max_cost_per_job_usd`, release lock and raise a fatal `cost_cap_exceeded` error.
   - Otherwise add the estimate to the local `cost_usd`, release lock, and proceed.
7. **Outside any DB transaction**, call `LLMProvider.translate_chunk(request)`.
8. On success:
   - Acquire `cost_lock`, adjust local `cost_usd` with actual cost from `ChunkResult`, release lock.
   - Save each returned translation via `TranslationCacheRepository.save_block_translation`.
   - Record a `ChunkAttemptRecord` with `outcome=AttemptOutcome.OK`.
9. On retryable error, let `Executor` retry from step 7.
10. On fatal error after exhaustion, record a `ChunkAttemptRecord` with `outcome=AttemptOutcome.FATAL_ERROR` and complete the chunk.
11. `JobExecutionRepository.complete_chunk(chunk_id)` outside the network call.

**Exit criteria:** Unit tests with `FakeProvider` prove cache hits avoid LLM calls, retries work, fatal errors are recorded, and the cost cap stops further provider calls.

---

## Task 5: Assembly (Cache-Driven)

**Files:**
- Create: `app/worker/assembly.py`

Implement `Assembly.render(...)`:

1. Get the original document path from `FileStorage`.
2. Load all blocks for the document.
3. Query the cache for every block using the job's `translation_key`.
4. Build `translations: dict[str, str]` from cached rows.
5. Any block without a cached translation is left untranslated; the renderer will render it as source text.
6. `format_registry.resolve(original_path)` → `(extractor, renderer)`.
7. Call `renderer.render(original_path, blocks, translations, output_path)`.
8. Save the rendered file via `FileStorage.save_output`.
9. Determine final status:
   - If **all** blocks have cached translations → `JobStatus.DONE`.
   - If **any** block is missing → `JobStatus.COMPLETED_WITH_ERRORS`.
10. Update job via `JobExecutionRepository.complete_job`.

**Important:** the decision between `done` and `completed_with_errors` is derived entirely from the cache, not from in-memory state.

**Exit criteria:** Integration test with `FakeProvider` and sample PDF/DOCX produces a translated file and correctly marks the job status.

---

## Task 6: Worker Entrypoint

**Files:**
- Create: `app/worker/__init__.py`
- Create: `app/worker/__main__.py`

`__main__.py`:
1. Load `Settings`.
2. Open SQLite connection via `SqliteConnectionFactory`.
3. Instantiate repositories, `FakeProvider`/`OpenAIProvider`, `ModelCostCalculator`, `FormatRegistry`, `FilesystemStorage`.
4. Create `ClaimLoop`.
5. Install `SIGTERM`/`SIGINT` handlers that set an `asyncio.Event` shutdown flag.
6. Run `ClaimLoop.run()` until shutdown.
7. Close the DB connection.

**Exit criteria:** `python -m app.worker` starts, idles, and shuts down cleanly.

---

## Task 7: Chaos / Resume Tests

**Files:**
- Create: `tests/worker/test_worker_resume.py`

Test scenario:
1. Create a document, job, and chunks using `FakeProvider`.
2. Start a `ClaimLoop` in a background task.
3. After some chunks commit, cancel the task and expire all leases.
4. Start a second `ClaimLoop` with a new `worker_id`.
5. Assert the job reaches `done` or `completed_with_errors`.
6. Assert `FakeProvider` was not invoked for blocks that already had committed translations in the cache.

**Exit criteria:** Chaos test passes.

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
- `DT-31`: worker executor with retry/backoff
- `DT-32`: claim loop and heartbeat
- `DT-33`: translation loop with cache, cost cap, and no-DB-lock rule
- `DT-34`: cache-driven assembly
- `DT-35`: worker entrypoint and chaos/resume tests

Single-commit option:

```bash
git add app/worker tests/worker app/config.py TASKS.md
git commit -m "DT-31: feat(worker): implement claim loop, executor, and cache-driven assembly"
```

---

## Critical Agent Reminders

1. **Single-job concurrency.** Process one job to completion before claiming the next. The semaphore limits only in-job chunk parallelism.
2. **No DB transaction across network calls.** `BEGIN ... COMMIT` must never wrap `LLMProvider.translate_chunk`. Open/close transactions only around cache writes, attempt records, and chunk/job state updates.
3. **Cache-driven assembly.** Do not track `failed_block_ids` in memory. Assembly decides `done` vs `completed_with_errors` by checking which document blocks have cached translations.
4. **Soft cost cap.** Use an `asyncio.Lock` to protect the worker's local `cost_usd` accumulator. A small overshoot is acceptable; the lock minimizes it.
5. **Heartbeats run in the background.** They renew job and chunk leases while chunks are executing under the semaphore.
6. **All blocking adapter calls are already threaded.** The worker itself is pure `asyncio`; it does not perform blocking I/O directly.

## Execution notes (2026-10-02)

Work uses branch `stage-4-worker` and sequential backlog tickets DT-26–DT-30.
The proposed DT-31–DT-35 numbers were provisional; TASKS.md remains authoritative.
The orchestrator delegated settings/executor/assembly, translation, and
claim-loop/entrypoint/recovery to three agents with disjoint file ownership;
a fourth agent independently reviewed the implementation after a slot freed.

Implementation corrections retain the approved public ports, domain models,
and database schema:

- `ChunkResult` carries token usage, not a cost field. Actual costs are calculated
  through the existing `CostCalculator` port. Retry attempts preserve known usage.
- The repository ports do not expose chunk membership or attempt history. An
  internal persistence helper reads these and coordinates reads with service
  transactions on the shared connection. SQL remains in the persistence adapter.
- Heartbeats continue through rendering and output publication. Stopping them
  before assembly, as the initial task ordering suggested, would allow duplicate
  claims of a long-running render.
- Chunk claims are bounded by available execution slots; valid inflight leases
  surviving a job restart are waited out and expired chunks are released during
  execution, not merely at startup.
- Success checkpoints atomically store cache rows, attempt usage, completion,
  and progress. Fatal or exhausted failures atomically store their diagnostic
  and terminal progress so a restart cannot repeat a fatal provider call.
- Partial cache hits send only missing blocks. Neighbor context uses source
  blocks outside the full chunk boundaries. Retry budgets include persisted
  attempt history across restart.
- Cost-cap rejection records a zero-usage fatal diagnostic without calling the
  provider. Final status follows cache coverage, including this failure case,
  consistent with the approved Stage 4 source-fallback policy.
- Missing document analysis uses a degraded source-side plan; triage itself
  remains a later stage. The entrypoint now honors the existing `LLM_PROVIDER`
  environment setting.

Review corrected successful-attempt latency measurement, fatal checkpoint
atomicity, lease release after startup, and resource cleanup when initialization
or provider shutdown fails. Filesystem publication and lease checks remain
separate operations through existing ports; concurrent worker publication races
under lease loss are documented in `docs/worker.md`, within the architecture's
single-worker scope.

Final acceptance: `make test` — **273 passed, 1 live test deselected**;
`make lint` — **clean, 86 files formatted**; `make typecheck` — **clean,
30 source files**. Worker coverage includes 31 tests plus two persistence
coordination tests. Restart testing closes/reopens the database, and process
testing launches `python -m app.worker`, waits for JSON readiness, and sends
SIGTERM. Independent review and scoped re-review found no blocking defects.
The last failed test expected one provider call per block despite batching all
blocks in one chunk; the expectation was corrected to the chunk count.
No live provider calls were made. The existing two Pydantic `register` warnings
remain. Commit hashes are recorded in TASKS.md after delivery.

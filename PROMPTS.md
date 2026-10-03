# PROMPTS.md

How AI was used to build this project — what was delegated, what was decided
by the human, and where AI output was rejected or corrected. Written for
judgment, not prompt volume, per the assessment brief.

**Working method:** the architecture was developed in dialogue with an LLM
coding assistant, then put under explicit adversarial review *before any code
was written*. The human's role in this phase: scope decisions, stack
confirmation, acceptance-criteria review, and the final call on every
contested point below. Implementation entries are appended as work proceeds.

---

## Log

### 2026-09-30 — Architecture brainstorming (design v1 → v3)

**Delegated to AI:** first-pass architecture for the whole service — queue
design, data model, pipeline, agent placement, MCP surface — proposed as
options with trade-offs, then redrafted twice under review.

**Rejected / corrected AI output:**

1. **The "zero duplicate billing" guarantee.** The initial design promised
   that a killed-and-resumed job's cost would equal the sum of unique chunk
   translations. Rejected as physically impossible over an external LLM API:
   an ambiguous timeout (provider processed the request, client never
   received the response) forces at-least-once provider invocation, and the
   Chat Completions API has no client idempotency keys. Replaced with an
   explicit guarantee boundary — exactly-once for *committed* results,
   at-least-once for *provider calls* — with duplicate spend measured in
   `chunk_attempts` and reported rather than hidden.

2. **Translated-context threading.** The initial design fed the previous
   chunk's *translation* into the next chunk's prompt for coherence — while
   simultaneously claiming 8-way chunk parallelism. Corrected: that is a
   serial dependency chain wearing a parallel costume. Coherence now comes
   from the persisted TranslationPlan + glossary + neighboring *source*
   blocks only; a translated-context polish pass is documented as a cut.

3. **Universal Document IR.** The initial format strategy normalized PDF and
   DOCX into one rich layout IR (typed blocks, style/geometry semantics).
   Rejected after a risk pass: over-engineering for a 3-day MVP, and style
   mapping degrades exactly where translated text changes length. The
   Markdown-bridge alternative was rejected alongside it (fatal layout loss).
   **Revised to the Opaque Metadata pattern:** the core handles only `seq` +
   `source_text`; format specifics travel as opaque JSON on the Block;
   renderers re-open the original file as the canvas. Full rationale in
   DECISIONS.md §3.

**Assessment of the collaboration pattern so far:** the AI was most useful
as a fast generator of complete, internally consistent first drafts — and
most dangerous exactly there: fluent, plausible guarantees that do not
survive contact with distributed-systems reality. Every invariant in the
final design either came from the human reviewer or survived an explicit
"what can this system actually promise?" cross-examination.

### 2026-10-01 — Workflow decision: architect/agent split, contracts deferred

**Decision.** Roles were separated explicitly. Architecture, contracts, and
decision records are maintained in Markdown with a human-in-the-loop gate
(ARCHITECTURE.md, DECISIONS.md, AGENTS.md, this file). Code is written by an
agent-driven implementation flow that starts from those artifacts.

**What happened.** The initial setup pass overshot: it produced a full
module skeleton with contract stubs (`core/ports.py`, `core/models.py`,
`core/errors.py`) before the implementation flow existed. The commit was
reverted. Contracts drafted in a vacuum — ahead of the flow that must own
them — either fossilize into dead scaffolding or get rewritten under the
first real constraint; both outcomes are worse than drafting them in the
moment, with the approval gate from AGENTS.md (rule 4) actually exercised.

**Correction logged.** "Scaffold early so agents stay in bounds" sounded
right and was still wrong *at this stage*: the correct bound at this stage
is the set of Markdown artifacts (ARCHITECTURE.md as SoT, AGENTS.md as
guardrails, pinned tooling), not premature Python contracts. Setup was
deliberately scoped down to: docs, guardrails, tooling, secret hygiene.

---

### 2026-10-02 — Approved API contracts implemented with orchestration

**User prompt (verbatim):**

```text
Задача для оркестратора. Имплементироват docs/plans/2026-10-02-api-contracts.md . При этом необходимо учесть следующее. Technical Alternatives (Transaction Management)

Чтобы гарантировать запись джоба и его чанков в одной SQLite WAL транзакции, у нас есть два пути:

Option A: Pragmatic / Coarse-grained Method (Рекомендуемый для MVP)

Вместо двух методов, мы декларируем один агрегирующий метод в интерфейсе порта:

async def create_job_with_chunks(self, job: JobRecord, chunks: list[ChunkRecord]) -> None:

Когда выбрать: Идеально для текущей задачи. Вся логика BEGIN ... COMMIT остается инкапсулированной внутри конкретной реализации SQLite-репозитория, а интерфейс ядра остается чистым.

Option B: Future-proof / Unit of Work (UoW)

Введение паттерна Unit of Work, который оборачивает вызовы репозиториев в контекстный менеджер (e.g., async with uow: uow.jobs.create_job(...); uow.jobs.create_chunks(...)).

Когда выбрать: Если система предполагает сложную оркестрацию множества агрегатов в разных таблицах. В нашем случае это добавит излишний boilerplate-код.

Actionable Recommendations

Approve and Proceed: Можешь отдавать план агенту в работу, дизайн утвержден.

Merge Create Methods: Укажи агенту использовать Option A и заменить create_job и create_chunks на единый create_job_with_chunks в JobExecutionRepository, чтобы гарантировать атомарность на уровне контракта.

Enforce Opaque Typing: В Block (строка 43) format_metadata определена как dict[str, Any]. Убедись, что при сериализации/десериализации Pydantic не будет пытаться валидировать или отбрасывать вложенные ключи. Поведение должно строго соответствовать типу Record<string, unknown>.

WAL Mode Initialization: Сделай пометку для реализации sqlite адаптера: при создании подключения (например, через aiosqlite) PRAGMA-инструкция journal_mode=WAL должна выполняться принудительно на старте приложения. -- Этот прромпт и коррекцию изначачального плана нужно будет зафиксировать в PRO
```

**Delegated to AI:** three parallel implementation agents own domain models and
ports (DT-6), REST schemas and adapter skeletons (DT-3), and contract/architecture
tests (DT-4). The coordinating agent owns documentation, integration review,
and final verification (DT-5). At implementation start, no commits were requested.

**Planning and agent roles:** the coordinating agent read the architecture and
approved plan, decomposed the work into tasks in `TASKS.md`, then assigned three
`worker` subagents explicit, disjoint file ownership: `core_contracts`,
`schemas_scaffolds`, and `contract_tests`. Implementation proceeded in parallel.
The coordinator reviewed the resulting files, requested corrections to tests
that required future PDF/DOCX adapters or compared Protocol internals, updated
the documentation, and resolved the final formatting failure.

**Human approval and corrections to the original plan:**

1. Replaced `JobExecutionRepository.create_job` and `create_chunks` with
   `create_job_with_chunks(job: JobRecord, chunks: list[ChunkRecord]) -> None`.
   Option A makes atomic enqueue a port obligation: the service initiates the
   aggregate write and the SQLite implementation owns BEGIN/COMMIT and rollback.
   Unit of Work was rejected for the MVP. The Database guardrail was updated to
   express this approved exception to service-managed transaction boundaries.
2. Kept `Block.format_metadata: dict[str, Any]` as an opaque JSON object. Nested
   unknown keys and values must survive Python and JSON Pydantic round trips;
   format-specific validation and normalization belong to the owning adapter.
3. Added an explicit persistence-stage requirement to execute
   `PRAGMA journal_mode=WAL` during startup and on every connection, together
   with `synchronous=NORMAL`, `foreign_keys=ON`, and `busy_timeout=20000`.
   This contract stage records that requirement; it does not implement SQLite.
4. Corrected the draft's missing `ChunkAttemptRecord` import and removed its
   unused `Path` import from the model example. Architecture references use
   section titles. The approved MCP signatures remain the design for the later
   MCP implementation stage; REST routes and concrete adapters are also deferred
   according to the plan's “Next Steps After Approval”.

**Log destination:** the prompt's trailing `PRO` was interpreted as this existing
`PROMPTS.md` AI-usage log.

**Result and verification:** approved models/ports, REST schemas, and the three
adapter skeletons are implemented. `make test`: 25 passed; `make lint` and
`make typecheck`: clean. Ruff also formats Python examples inside the plan's
Markdown fences. Pydantic warns that the approved `TranslationPlan.register`
field shadows `BaseModel.register`; the approved field name is preserved.
DT-3 through DT-6 are done. The user subsequently authorized commits. During
commit preparation, the coordinator found DT-2 in existing Git history but
missing from the backlog, restored that historical task, and assigned the core
contract work DT-6 to avoid reusing a ticket number.

**Commit preparation correction:** pre-commit uses Ruff v0.8.4, which flagged
tuple-based `isinstance` checks and reformatted two assertions even though the
installed project Ruff had passed. The coordinator changed those tests to
union-based `isinstance` checks and assertions accepted by both formatters.
All 25 tests and `make lint` passed again; the test commit then passed pre-commit.

### 2026-10-02 — Planning core models, ports, and persistence schema

**User prompt (paraphrased):** Plan implementation for `app/core/models.py`
(final Pydantic code), `app/core/ports.py` (using the aggregate
`create_job_with_chunks` method), and `app/adapters/persistence/schema.sql`
(tables with claim-loop indexes). Put each plan in a separate Markdown file.

**Delegated to AI:** draft the three implementation plans, including final code
for each artifact, and update the repo-local backlog.

**What the AI proposed:**

1. **Chunk-block membership in the aggregate write.** Two options were offered:
   - *Option 1:* extend `ChunkRecord` with an optional `block_ids` field so the
     persistence adapter could derive `chunk_blocks` from the chunk records.
   - *Option A:* keep `ChunkRecord` strictly 1:1 with the `chunks` table and add
     an explicit `chunk_blocks: list[ChunkBlockRecord]` parameter to
     `create_job_with_chunks`.
2. **Strict Pydantic configuration:** apply `ConfigDict(extra="forbid")` to all
   core models to reject unknown fields early.
3. **Persistence schema:** create `app/adapters/persistence/schema.sql` with all
   tables from `ARCHITECTURE.md` §5 and indexes optimized for the worker claim
   loop (`idx_jobs_claim`, `idx_chunks_claim`, `idx_chunks_expired`,
   `idx_block_translations_lookup`).

**Human approval and corrections:**

1. **Rejected Option 1.** `ChunkRecord` must map 1:1 to the `chunks` table
   columns. `block_ids` must not leak into the record model.
2. **Adopted Option A.** `JobExecutionRepository.create_job_with_chunks` now
   takes three parameters:
   `create_job_with_chunks(job, chunks: list[ChunkRecord],
   chunk_blocks: list[ChunkBlockRecord]) -> None`. This preserves table-record
   fidelity while keeping the aggregate transaction boundary explicit in the
   port contract.
3. **Approved strictness.** `ConfigDict(extra="forbid")` confirmed for every
   model.
4. **Approved schema.** DDL and indexes accepted without changes.
5. **Language and scope.** Plans were written in English. Only documentation
   (`docs/plans/...`) and the backlog (`TASKS.md`) were modified; no
   implementation files were changed.

**Result:** three approved plans created —
`docs/plans/2026-10-02-core-models.md`,
`docs/plans/2026-10-02-core-ports.md`, and
`docs/plans/2026-10-02-persistence-schema.md`. `TASKS.md` updated with
`DT-7`, `DT-8`, and `DT-9` as Stage 1 todo items for the orchestrator.

### 2026-10-02 — Orchestrated implementation of the three approved Stage 1 plans

**User request:** continue as orchestrator, plan and decompose implementation of
the three new files in `docs/plans/`, delegate the work, validate it, and create
separate commits for each task.

**Planning and delegation:** existing DT-7–DT-9 were selected and marked
in-progress before code changes. DT-10 covers integration, documentation, and
validation. Three parallel `worker` subagents have disjoint file ownership:
`models_finalization` owns models and new strict-model tests;
`ports_finalization` owns ports and their contract tests;
`persistence_schema` owns the DDL/package and schema tests. DT-8 depends on DT-7's
new record types; schema/model parity checks also wait for those types. The main
agent coordinates dependencies, reviews changes, and creates the commits.

**Workflow adaptation:** plans refer to `superpowers:executing-plans`, which is
not installed in this session. Their approved implementation steps are executed
with the available collaboration tools. No provider calls are needed.

**Review decisions:** strict model tests construct valid records first and
assert `extra_forbidden` specifically; the draft JobRecord test omitted required
nullable fields and could pass for unrelated missing-field errors. The ports
plan's final code also changes `get_analysis` to return the persisted analysis
record, which is included although its task prose only names `save_analysis`.
The approved DDL is preserved; runtime repository methods and startup wiring
remain later stages. Schema tests configure all four required PRAGMAs on
file-backed temporary SQLite connections and validate constraints as well as
table/index definitions.

**Integration review correction:** the first schema rollback test used an
already occupied `seq_in_chunk` for its missing-block insert, so a uniqueness
failure could masquerade as the intended foreign-key failure. The coordinator
requested a distinct sequence and an assertion of the foreign-key error cause.
The agent also added focused uniqueness tests and proved rollback of a whole
job/chunks/join-row transaction while preserving previously committed rows.

**Validation:** `make test`: 79 passed (including 20 schema tests);
`make lint`: clean after formatting the two new test files;
`make typecheck`: clean, 14 source files. There are two Pydantic warnings for
the approved `register` fields on `TranslationPlan` and `DocumentAnalysisRecord`;
field names are preserved. No real LLM calls were made. Concrete persistence
repositories and application startup PRAGMA wiring are outside these three plans.

**Delivery:** separate implementation commits were created for DT-7 (`cac742f`),
DT-8 (`ffd8cd5`), and DT-9 (`5c9777b`), each containing its tests and approved
plan. All commit hooks passed. DT-10 records integration/documentation and task
hash backfills. The pre-existing untracked `TEST_TASK.md` remains outside this
stage's commits. No history was amended or rebased.

### 2026-10-02 — Stage 1 persistence plan corrections

**User request:** refine the Stage 1 implementation plan before execution and
record the corrections.

**Corrections to the draft plan:**

1. **Direct `aiosqlite` approved.** SQLAlchemy was rejected for the persistence
   layer; raw SQL via `aiosqlite` matches the existing `schema.sql` and is the
   fastest path for the MVP.
2. **Active connection injected into repositories.** Repositories must be
   initialized with `__init__(self, connection: aiosqlite.Connection)`; the
   factory/application layer owns connection open/close. Opening a connection
   inside every repository method is forbidden.
3. **Parameterized queries only.** All SQL must use `?` placeholders; string
   concatenation or f-strings in SQL are disallowed.
4. **UTC datetimes.** All stored datetimes must be UTC ISO 8601 strings to avoid
   lease expiry bugs in `release_expired_chunks`, `claim_job`, and `claim_chunk`.
5. **FilesystemStorage path traversal protection.** `os.path.abspath` plus a
   base-directory check is required.

**Note:** the initial correction was mistakenly written to `DECISIONS.md`; it
was removed from there and logged in this file, which is the correct place for
AI-usage and correction records. The approved Stage 1 plan in
`docs/plans/2026-10-02-stage-1-persistence.md` already reflects all five
points.

### 2026-10-02 — Orchestrated Stage 1 persistence implementation

**User request:** execute `docs/plans/2026-10-02-stage-1-persistence.md` with
planning, decomposition, delegation, review, validation, and commits.

**Decomposition:** DT-11 settings; DT-12 connection factory and transaction
boundary; DT-13 repositories; DT-14 filesystem storage; DT-15 integration,
review, and documentation. Previously completed DT-7–DT-9 are not reused.

**Delegation:** three `worker` agents have distinct ownership:
`persistence_foundation` owns Settings, the factory, and their tests;
`sqlite_repositories` owns all three repository implementations and contract
tests; `filesystem_storage` owns the storage adapter and its tests. The main
agent owns independent integration tests and review, documentation, full checks,
and commits. Repository work depends on the foundation's agreed transaction
helpers; filesystem work proceeds independently. Installed aiosqlite source was
read before implementing cursor and connection APIs.

**Corrections made before implementation:** the factory test's in-memory WAL
assertion was impossible, and cursor fetch calls were not awaited. Acceptance
tests now use file-backed databases and proper async cursor contexts. The
filesystem helper's string-prefix containment accepted sibling directories;
canonical path-component containment replaces it. Ordinary writes require a
caller-owned transaction, while aggregate enqueue owns its transaction. Internal
transaction helpers handle cancellation and serialize sharing of a connection.
The queued-only claim example was extended to recover expired running/assembling
jobs per the architecture, preserving assembling status. Job cost/usage is
updated with every attempt in the same transaction. These are implementation
corrections; no core models, ports, or DDL are changed.

**Independent review and corrections:** a fourth, read-only explorer agent,
`persistence_review`, reviewed transaction cancellation, shared-connection
ownership, and leases. Foreign tasks now cannot read or write through a
connection owned by another managed transaction. Cancellation before COMMIT
rolls back; after COMMIT starts, its completion is shielded and a successful
commit is reported as success. An optional concrete `worker_id` constructor
binding fences job/chunk mutations to live owner leases, including parent jobs.
Worker incarnations must have distinct IDs. Unscoped instances remain available
for administrative operations; attempt costs remain recorded after lease loss.
The reviewer found no remaining defect in these corrections.

**Validation:** the main agent added eight integration tests covering two real
WAL connections, concurrent claims/enqueue/cache, rollback, persistence across
reopen, lease recovery, connection ownership, and cancellation. `make test`
passed 145 tests with the two existing Pydantic `register` warnings;
`make lint` and `make typecheck` passed. The sandbox stalled aiosqlite and
thread-backed filesystem I/O, so acceptance tests were rerun with authorized
execution outside the sandbox using real connections and threads. No live LLM
calls were made. Separate commits cover DT-11–DT-15; unrelated existing changes
are excluded.

### 2026-10-02 — Stage 2 LLM provider plan corrections

**User request:** approve the Stage 2 implementation plan subject to four
important corrections before handing it to the orchestrator.

**Approved corrections:**

1. **Source-side context in `ChunkRequest`.** Approved adding optional
   `context_before: list[Block]` and `context_after: list[Block]` to
   `ChunkRequest` so the provider can include neighboring source blocks in the
   prompt without returning translations for them.
2. **OpenAI Structured Outputs.** The agent must use OpenAI Structured Outputs
   (`client.beta.chat.completions.parse` with a Pydantic schema, or
   `response_format={"type": "json_schema"}`) instead of free-form JSON mode.
   This prevents hallucinated or missing translation keys.
3. **Tiktoken encoder caching.** `tiktoken.encoding_for_model` must not be
called per chunk. The encoder is initialized once at module or class level and
reused across calls.
4. **Missing block validation.** `OpenAIProvider` must verify that every
   `block.id` from the requested chunk is present in the model response. Any
   missing block is treated as a **retryable** error.

**Result:** the approved plan was written to
`docs/plans/2026-10-02-stage-2-llm-provider.md` with the four corrections
embedded as explicit agent reminders and implementation requirements.

### 2026-10-02 — Stage 3 format adapter plan approvals

**User request:** approve the Stage 3 implementation plan and provide additional
architectural rules for the orchestrator.

**Approved decisions:**

1. **DOCX granularity — paragraph-level blocks.** The renderer clears all
   existing runs in a translated paragraph, inserts a single new run with the
   translation, and preserves only the paragraph-level style
   (`paragraph.style`). Run-level formatting inside a paragraph is intentionally
   discarded; the LLM does not receive markup, so reconstructing runs would be
   unreliable.
2. **PDF strategy — bbox insertion + auto-shrink + fallback page.** Translated
   text is rendered into the original bbox with auto-shrinking font down to a
   minimum readable size. If it still does not fit, the renderer creates a new
   page for that block and records the fallback.

**Additional architectural rules:**

- **Async discipline:** every interaction with `fitz` (PyMuPDF) and
  `python-docx` (open, iterate, insert, save) must run inside
  `asyncio.to_thread` to avoid blocking the event loop.
- **Security:** `FormatRegistry.resolve` reads only the first 2048 bytes of a
  file for magic-byte validation; the full file is never loaded just to detect
  its format.

**Result:** the approved plan was written to
`docs/plans/2026-10-02-stage-3-format-adapters.md` with the two decisions and
both guardrails embedded as explicit implementation requirements.

### 2026-10-02 — Stage 3 delegated execution and review

**User request:** plan, decompose, delegate, execute, and verify the Stage 3
format-adapter plan, then commit the completed work.

**Delegation:** three workers owned PDF plus unit tests, DOCX plus unit tests,
and registry/sample generation plus registry tests. The orchestrator owned
the shared error catalog, opaque metadata and sample integration tests,
overflow measurements, documentation, acceptance checks, and delivery.
Workers read architecture and installed library source before implementation.
Completed DOCX and registry workers independently reviewed other components.

**Rejected/corrected output:** Linux-only font discovery with a Helvetica
fallback could silently corrupt non-Latin text; use a bundled Unicode font
with glyph checks. A 20-character scanned-PDF threshold rejected short valid
text documents; detect absence of usable text instead. Renderer rejection of
slightly off-page bboxes from its own extractor was corrected within the PDF
adapter. Empty supplied translations and fractional font-size shrinking were
reviewed to ensure consistent source removal and a real 6 pt attempt.

**Scope made explicit:** DOCX translates top-level paragraphs and preserves
tables/headers/footers unchanged. PDF table rows may move to appended pages;
layout measurements are reported separately from translation quality. The
registry's ZIP signature is a routing hint; DOCX package parsing performs the
actual validation. No new public models, ports, or endpoints were introduced.

**Tooling:** the plan's `superpowers:executing-plans` skill was unavailable;
native task delegation and repository tools executed its steps. Threaded I/O
tests ran outside the tool sandbox after standard async tests stalled inside.
Final verification and measurements are recorded in TASKS.md and DECISIONS.md.

### 2026-10-02 — Stage 4 worker plan corrections

**User request:** approve the Stage 4 implementation plan and provide strict
corrections for the orchestrator.

**Approved decisions and corrections:**

1. **Single-job concurrency.** The worker processes exactly one job at a time.
   Bounded parallelism (semaphore 8) applies only to chunks within the current
   job. This prevents OOM and simplifies lease/heartbeat management.
2. **Cache-driven assembly — reject in-memory `failed_block_ids`.** The worker
   must not keep failed-block state in memory. After all chunks finish, assembly
   reads the cache for the job's `translation_key`. Blocks without cached
   translations are rendered as source text, and the job is marked
   `COMPLETED_WITH_ERRORS` if any are missing. This survives `kill -9` because
   the cache is the single source of completion truth.
3. **No SQLite transaction across network calls.** `BEGIN ... COMMIT` must never
   span an `LLMProvider.translate_chunk` call. The provider call runs outside
   any DB transaction; only the result (cache writes, attempt records, state
   updates) is persisted afterwards.
4. **Soft cost cap with `asyncio.Lock`.** The worker uses an in-memory
   `asyncio.Lock` to protect its local `cost_usd` accumulator while chunks under
   the semaphore check the cap and add estimated costs. The cap is a soft limit;
   the lock minimizes overshoot.

**Result:** the approved plan was written to
`docs/plans/2026-10-02-stage-4-worker.md` with all four rules embedded as
explicit implementation requirements.

### 2026-10-02 — Stage 4 worker orchestration and independent review

**User request:** plan, decompose, delegate, implement, verify the Stage 4 plan,
and commit the completed work.

**Delegation:** separate agents owned settings/executor/assembly, translation,
and claim loop/entrypoint/recovery tests. The orchestrator owned internal
SQLite coordination, semantic cache keys, error catalog updates, documentation,
acceptance checks, and commits. A separate reviewer inspected lease ownership,
transactions, cancellation, cost accounting, and final assembly.

**Rejected/corrected output:** stopped heartbeat-before-assembly ordering;
successful attempt latency initially persisted as zero; terminal failure
recording initially left a crash window before chunk completion; recovered
chunks whose leases expired after startup were initially never released;
entrypoint cleanup initially began after provider construction; a progress
update was accidentally indented beneath a raising branch. Each issue was
corrected before delivery. The reviewer's suggested job-renewal leak on chunk
lease loss was disproved: the encompassing transaction rolls renewal back.

**Scope:** approved public models, ports, and DDL remain unchanged. Real OpenAI
calls are excluded from tests. The existing Stage 4 plan corrections above are
preserved; unrelated user files are excluded from the worker delivery.

**Final verification:** `make test` — 273 passed, 1 live test deselected;
`make lint` — clean; `make typecheck` — clean (30 source files). Tests include
real-format assembly, atomic rollback, concurrent cap reservations, an independent
WAL writer during provider waits, heartbeat during rendering, SQLite reopen
recovery, and an actual worker process stopped by SIGTERM after JSON readiness.
The final test correction counts provider calls per chunk rather than per block.
Two existing Pydantic warnings remain; no live calls were made.

### 2026-10-02 — Stage 5 REST API plan corrections

**User request:** approve the Stage 5 implementation plan and provide strict
architectural corrections.

**Approved decisions and corrections:**

1. **Fat-controller prevention.** Chunking, token counting, and job
   orchestration logic must not live in `routers/jobs.py`. The agent must create
   `app/core/services/job_service.py` and encapsulate that business logic there.
   The router remains a thin translation layer between HTTP and the service.
2. **Triage stub.** Until Stage 6, `POST /api/documents` must synchronously
   create a default `DocumentAnalysisRecord` through the repository. This makes
   the full API flow testable end-to-end and will be replaced by the background
   triage task in Stage 6.
3. **Worker heartbeat deferred.** `/readyz` checks only SQLite connectivity and
   storage writability. No heartbeat table is added; worker-heartbeat freshness
   is deferred to Stage 9.
4. **SSE disconnect handling.** The SSE events generator must check
   `await request.is_disconnected()` inside the polling loop and break if the
   client disconnects, preventing infinite DB polling and resource leaks.

**Result:** the approved plan was written to
`docs/plans/2026-10-02-stage-5-rest-api.md` with all four corrections embedded
as explicit implementation requirements.

### 2026-10-02 — Stage 6 triage agent plan corrections

**User review:** the original Stage 6 plan correctly isolated triage from the
HTTP request via FastAPI `BackgroundTasks`, but the proposed tools were too
weak and the in-memory task created a stuck-state risk.

**Approved corrections:**

1. **Researcher-style tools.** Removed `detect_language` and `classify_domain`
   tools. The agent now uses only navigation tools:
   - `read_blocks(start_seq, count)` — read a slice of document blocks.
   - `search_blocks(keyword)` — find blocks containing a keyword.
   The agent justifies its existence by investigating long documents that do
   not fit in a single context window.
2. **Chain-of-thought output schema.** Added `TriageAgentOutput(BaseModel)`
   with `reasoning: str` and `plan: TranslationPlan`. The reasoning field forces
   the model to think before emitting the final structured plan, reducing
   hallucinations.
3. **Stuck-state recovery.** Added `POST /api/documents/{id}/retry-triage` so
   a document left in `analyzing` after a `kill -9` can be manually restarted.
4. **Background task DB connection.** `run_triage` opens its own SQLite
   connection through `SqliteConnectionFactory`; it cannot reuse the
   request-scoped connection.

**Trade-off logged.** FastAPI `BackgroundTasks` were kept for the MVP because
they add zero infrastructure overhead, but they are not resilient to process
kill. The `retry-triage` endpoint is the chosen mitigation; a durable triage
queue owned by the worker is listed in `DECISIONS.md` as a future improvement.

### 2026-10-02 — Stage 7 MCP plan corrections

**User review:** the initial Stage 7 plan proposed using an HTTPX client to call
the application's own FastAPI service. The user rejected that approach to
preserve the requirement that REST and MCP are two front doors to the same core.

**Approved corrections:**

1. **Direct core composition.** MCP tools call `DocumentService`, `JobService`,
   `TriageService`, repository ports, and `FileStorage` directly. No HTTPX
   client to the local FastAPI service and no SQL in MCP tools.
2. **Shared-directory sandbox.** The MCP container receives a dedicated host
   directory mount for input/output. Every path is resolved and checked for
   containment (including symlinks); arbitrary host paths are inaccessible.
3. **Recent-jobs contract.** Add `JobService.list_recent_jobs(limit)` and expose
   it through `GET /api/jobs?limit=10`, backed by the same service query.
4. **Local triage polling.** `translate_file` waits on document status through
   `DocumentRepository` (or `DocumentService`), using short-lived connections,
   until `EXTRACTED`/`FAILED`; it then creates the job through `JobService`.
   No REST endpoints are polled.

**Result:** the revised plan is in
`docs/plans/2026-10-02-stage-7-mcp-server.md`; the matching trade-offs are
recorded in `DECISIONS.md` §8.

### Stage 7 MCP execution (2026-10-02)

User approved the Stage 7 plan, secure shared-path resolver, atomic triage claims
across REST/MCP and a maximum 45-second polling deadline; requested orchestration,
delegation, validation and a final commit. Root divided work into DT-41–DT-46,
assigned recent jobs, shared triage and MCP implementation to separate agents,
and retained config/path validation, docs, real HTTP verification and delivery.
Implementers used the existing services directly and inspected installed FastMCP.
An independent reviewer examined concurrency, cancellation, paths and lifecycle.

Rejected the old process-local triage lock as cross-process protection and an
unconditional analyzing-to-analyzing update as a unique claim. Shared advisory
ownership accompanies the service's conditional update, with crash recovery
without schema changes. Review also required cleanup for cancellation before a
background task's first execution and restoration of an analyzing record with an
existing successful plan. Shared upload ownership protects duplicate-content
artifacts from a losing ingestion cleanup. Timeout recovery uses the returned
document ID with check_status, followed by idempotent resubmission when extracted.
All project LLM checks remain offline using fake providers.

### 2026-10-02 — Stage 8 frontend plan corrections

**User review:** the initial Stage 8 plan polled `POST /api/jobs` for
`409 analysis_pending` and left branding unresolved. Both were rejected.

**Approved corrections:**

1. **Stop POST polling.** Polling `POST /api/jobs` to detect readiness is an
   antipattern. Add `GET /api/documents/{id}` to `app/api/routers/documents.py`,
   backed by a `DocumentService` read operation. The frontend polls document
   status and issues exactly one `POST /api/jobs` after status becomes
   `extracted`.
2. **Safe SPA fallback.** The `index.html` catch-all in `app/api/main.py` must
   be registered strictly last, after every API router, and must exclude
   `/api` and `/api/...` so a mistyped API path returns a structured JSON 404
   instead of the React shell.
3. **Two dev terminals approved.** `make dev` stays for FastAPI; a new
   `make frontend-dev` runs Vite. The README documents the two-terminal flow.
4. **Branding source fixed to `starkfuture.com`.** A first draft would have
   resolved branding from `getstark.co`, which is a different company with an
   unrelated teal/purple palette. Verified values from the official production
   CSS: Stark red `#FF1717`, black `#000000`, dark neutrals `#242424` and
   `#1E1E1E`. No official semantic "secondary" token exists, so `#242424` is
   recorded as an application surface alias rather than an official brand
   color. The wordmark SVG is downloaded into the repository; remote asset
   hotlinking is forbidden.

**Result:** the revised plan is in `docs/plans/2026-10-02-stage-8-frontend.md`;
the branding and fallback decisions are recorded in `DECISIONS.md` §9.

### 2026-10-02 — Stage 8 frontend execution

The user approved Stage 8 and requested orchestration, delegation, implementation,
verification, and commits. Additional requirements: React 18 SSE cleanup;
FastAPI custom 404 SPA fallback protecting API JSON; client MIME checks.

Root decomposed DT-47–DT-57 and delegated scoped scaffold, typed client,
document-status service/router, upload/readiness, job/SSE, History, branding,
static serving, and workflow docs. Separate reviewers checked specification
and quality after each task. A QA agent prepared a real Chromium acceptance
script; root owns its execution, full checks, integration review, and delivery.
See [Stage 8 execution record](docs/plans/2026-10-02-stage-8-frontend-execution.md).

Rejected/corrected output: initial vulnerable Router/Vitest selections were
updated while retaining React 18/Tailwind 3; a retry left History filters on old
snapshots, fixed with card-to-list status synchronization; static fallback
reserved assets but missed extensionless branding resources, fixed with explicit
branding namespace protection. Browser acceptance then caught real dotted batch
IDs being misclassified as file paths on refresh; exact detail routes now keep
SPA fallback, covered with the actual ID shape. Review and regression tests
verified these corrections.
Root downloaded the official wordmark locally and recolored only path fills.
Readiness uses bounded GET polling and a stable key for explicit job retries;
SSE uses abort/revision/connection guards and closes on cleanup/terminal state.
The existing failed document shape has no diagnostics, so recovery text stays
safe and generic rather than inventing fields. All LLM validation uses fake
providers. Existing user changes outside Stage 8 are preserved.

Final integration review used the actual production bundle in Chromium with
an eight-language queued batch and reproduced the HTTP/1.1 connection limit:
six unbounded EventSources stalled ordinary GETs. The fix coordinates four
streams per application page, with cancellable GET polling and slot promotion
for other active jobs, preserving the existing public endpoints. Focused
capacity tests and browser evidence are included in the delivery record.

### 2026-10-03 — Stage 9 implementation and review

The user requested orchestration, delegation, implementation, verification, and
commits for the approved end-to-end/chaos/observability plan. Root delegated
container/chaos delivery (DT-58/59), stale-lease readiness (DT-60), and live
measurement tooling (DT-61) to three agents with disjoint file ownership. Root
owned integration, CI/runbook, live evidence, acceptance, and delivery.

Corrections during integration: Dockerfile HEALTHCHECK uses Docker `CMD`;
SQLite ISO lease comparisons in the shell script use `julianday`; already-pending
chunks may legitimately gain attempts after restart; the durable table cannot
count an interrupted request whose outcome was never checkpointed. Tokenizer
files are fetched during image build so fake-provider runtime works offline.
Measurement polls document readiness after upload's `analyzing` response and
uses standard per-order chrF precision/recall averaging, verified against the
primary SacreBLEU implementation. The command supports explicit dotenv loading
through Settings and ignores legacy/Compose-only keys without printing secrets.

Readiness tests passed; an independent measurement review found no blocking
issues. The final reviewer raised a missing cost-cap setting, then withdrew
the finding after checking the current Compose file with `rg`; it already
passes `MAX_COST_PER_JOB_USD`. Full offline checks and concrete container
acceptance are recorded in the Stage 9 execution record.

One real OpenAI sample PDF run completed in both directions; raw machine
evidence is committed under `docs/measurements/`. Reported costs cover bulk
attempts; triage usage is unrecorded. Forward triage succeeded after retries,
reverse triage degraded, and those limits are stated beside the actual score.
Population job p95 and parallelism comparison remain explicitly unmeasured.
Independent container acceptance discovered a real integration regression:
MCP returned a completed download owned by container UID 10001 with mode 0600,
so the host could not open it. DT-65 corrects final publication permissions
while keeping the temporary copy private and the rename atomic.
Existing user-authored files/approval notes are preserved outside these commits.

### 2026-10-02 — Stage 9 end-to-end plan rulings

**User review:** the draft plan proposed a `FAKE_INVOCATION_LOG` on disk, was
unsure about spending money on live provider runs, and re-opened the worker
heartbeat question that was already settled. All three were corrected.

**Approved rulings:**

1. **No provider instrumentation for chaos.** `FakeProvider` stays clean. The
   chaos script proves recovery with the `sqlite3` CLI against the durable tables
   that already exist: `chunk_attempts`, `block_translations`, and `chunks`.
   Counters are captured before the kill and after completion.
2. **Stateless worker-liveness heuristic in `/readyz`.** No heartbeat table and
   no marker files. Readiness returns 503 when chunks remain `inflight` with a
   `lease_expires_at` older than the 120 s grace period. An idle system reports
   ready; that limitation is documented rather than hidden.
3. **Live measurements are mandatory.** `scripts/measure_quality.py` and the
   final `DECISIONS.md` numbers must come from one real OpenAI run. CI and the
   automated suite keep using `FakeProvider`. Unmeasured figures are written as
   "not measured" with a reason, never invented.

**Pushback recorded — validated and endorsed by the reviewer.** The ruling
"the attempt count must not increase" was corrected: a chunk killed while
`inflight` has no committed translation and is re-executed by design, so
`chunk_attempts` grows for exactly that chunk. The provable invariant is that
already-committed translations are never re-requested and that
`block_translations` never gains duplicates. The reviewer confirmed this is the
only correct reading: exactly-once for committed business results,
at-least-once for provider invocations under an ambiguous failure. The script
prints both numbers and labels which guarantee each one demonstrates.

**Final approvals:**
- Installing the `sqlite3` CLI in the production image is approved as a
  deliberate compromise for this assessment; verification speed outweighs a
  minimal image surface here.
- The fail-loudly rule for `scripts/measure_quality.py` is confirmed: it must
  exit non-zero under `LLM_PROVIDER=fake` so an offline run can never be
  mistaken for a measurement.
- Documenting the `/readyz` heuristic limitations is approved as written.

**Implementation note.** `python:*-slim` does not ship the `sqlite3` CLI, so the
approved verification approach requires installing it in the runtime image.

**Result:** the plan is in
`docs/plans/2026-10-02-stage-9-e2e-chaos-observability.md`; the readiness
decision is recorded in `DECISIONS.md` §10.

### 2026-10-02 — Stage 10 submission-prep rulings

**User review:** the Stage 10 plan identified two things worth fixing before
delivery — documentation drift inside `DECISIONS.md`, and the fact that the
reviewer had to follow internal links to reach the brief's explicit questions.

**Approved rulings:**

1. **Documentation drift is a real defect.** `DECISIONS.md` §6 still advertised
   "pending implementation" measurements while §9 already reported them. A
   reviewer reading both cannot tell which is current. Resolve it.
2. **README carries the reviewer-facing answers.** Add an architecture summary,
   "who this is for", the three acceptance criteria, and a requirement map
   directly in the README. The small duplication with `ARCHITECTURE.md` is an
   accepted tradeoff: reviewer convenience outranks strict DRY in a submission
   document. The section header must use the exact phrase **"Where an agent
   earns its keep"** so the answer to the brief's question is instantly
   recognizable.
3. **Docker build belongs in CI.** It lengthens the pipeline, but image
   buildability is a hard requirement and nothing else would catch a broken
   `Dockerfile` before a reviewer runs it.
4. **Git lifecycle is human-owned.** No remote, no push, no rebase/amend/squash.
   Repository publication and history compression stay with the human.
5. **Assessment artifacts are relocated.** `TEST_TASK.md`, `docs/roadmap.md`, and
   the uncommitted Stage 9 plan move to `docs/assessment_context/`, keeping the
   root about the product while preserving the assessment record. Historical
   execution records that mention the old paths are left untouched: they are
   accurate statements about their moment, and rewriting them would be its own
   kind of falsification. Only live navigational links are updated.

**Result:** the plan is in
`docs/plans/2026-10-02-stage-10-submission.md`; artifacts were relocated and the
backlog pointer was updated.

### 2026-10-03 — Stage 10 delegated execution and review

**Request:** execute the approved submission plan with orchestration,
delegation, verification and commits.

**Delegation:** separate agents owned README reviewer answers/testing guide;
DECISIONS, Docker CI and assessment packaging; and isolated clean-clone
Compose/MCP acceptance. A fourth agent independently reviewed the combined
documentation and CI changes. The orchestrator owned task registration,
integration, submission evidence and delivery.

**Corrections to generated output:** the first README draft removed the
runnable Compose quickstart while consolidating commands. Review restored it
because setup instructions need to remain directly usable. The requirement
map also needed explicit PDF web delivery and restart recovery rows; the
pre-commit command was changed to `uv run pre-commit` for the local environment.
Review required the editor-client result to remain distinct from successful
MCP protocol checks; an isolated configuration does not prove a pristine
client installation.

Final review also rejected the architecture's claim that all ambiguous provider
billing is measured. The documentation now distinguishes known persisted bulk
attempt cost from unknown timeout/uncheckpointed usage and unrecorded triage
spend, and does not claim an unperformed scaling comparison.
The same review aligned cache-hit instrumentation and population-p95 claims
with the explicit unmeasured limits, retaining the intended acceptance goals.

**Result:** measured-number pointers replace contradictory placeholders;
unmeasured quality, billing and latency limits remain explicit. CI builds the
image without provider credentials. The literal submission checklist, fresh
test counts and runtime/client outcomes are recorded in
[Stage 10 verification](docs/plans/2026-10-02-stage-10-submission-verification.md).
No public contracts changed. Existing user-written Stage 10 rulings are
preserved as a separate uncommitted change; this execution entry is the only
new PROMPTS content included in the delivery commit.


## DT-93 — triage efficiency and analysis cost (2026-10-03)

The owner requested orchestration, delegated implementation, verification and
commit of the triage-efficiency plan. Three agents owned backend limits/retry,
cumulative document cost and frontend presentation; an independent reviewer
checked spec and quality across all changes. Root owned integration and docs.

Rejected plan text: checking only the new terminal flag would still retry auth
failures; triage stops on `not retryable or terminal`. The shared error catalog
stays unchanged. DT-92 was already occupied, so this delivery uses DT-93.
Fresh upload cost is zero; duplicate upload and retry preserve recorded totals.
No invalid-key live call or measured latency savings are claimed. ASGI smoke
with FakeProvider observed $0.000038775 analysis cost after extraction.

Verification and token telemetry are recorded in
[DT-93 execution](docs/plans/2026-10-03-triage-agent-efficiency-execution.md).

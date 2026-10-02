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

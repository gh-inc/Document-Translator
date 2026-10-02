# TASKS.md — Backlog & Task Log

Ticket source of truth for commit messages (see
[CONTRIBUTING.md](./CONTRIBUTING.md)). Prefix: **DT**. Numbers are
sequential and never reused, even if a task is cancelled.

The assessment brief lives at
[docs/assessment_context/TEST_TASK.md](docs/assessment_context/TEST_TASK.md);
the staged delivery roadmap is at
[docs/assessment_context/roadmap.md](docs/assessment_context/roadmap.md).

## How agents use this file

1. Pick the next `todo` task — or, before starting a stage, decompose it
   into tasks numbered `DT-<next sequential>` and list them here first.
2. Mark the task `in-progress` when you start; reference it in every commit
   (`DT-<N>: <type>(<scope>): ...`).
3. When finished, mark `done` and backfill the commit hash(es).
4. One task = one logical, reviewable change. If a task grows, split it —
   never renumber; new tasks get new numbers.

Statuses: `todo` → `in-progress` → `done` (or `cancelled`, with reason).

## Stages (implementation order, per ARCHITECTURE.md)

0. Repository conventions & docs
1. Persistence layer (schema.sql, repositories, WAL pragmas)
2. LLM port + FakeProvider + OpenAIProvider
3. Format adapters: PDF and DOCX (extractor + renderer)
4. Worker: leases, claim loop, bounded-parallel executor, retry/backoff
5. REST API + SSE
6. Triage agent (openai-agents SDK) + degraded fallback
7. MCP server
8. Frontend (React, Stark Future branding)
9. Compose delivery, chaos script, and remaining acceptance measurements

## Tasks

| ID | Stage | Title | Status | Commits |
|----|-------|-------|--------|---------|
| DT-1 | 0 | Establish git conventions and task backlog | done | 8590e81, this fix |
| DT-2 | 0 | Record AI provider and cost strategy; correct decision references | done | 095b67b, 8094265 |
| DT-3 | 0 | Add REST schemas and adapter skeletons | done | 8669e64 |
| DT-4 | 0 | Verify opaque metadata and contract architecture invariants | done | 412af38 |
| DT-5 | 0 | Record approved contract corrections and persistence requirements | done | 10e7793 |
| DT-6 | 0 | Implement approved domain models and atomic repository ports | done | 6837492 |
| DT-7 | 1 | Finalize strict Pydantic core models (1:1 record-to-column) | done | cac742f |
| DT-8 | 1 | Finalize core repository ports with aggregate create_job_with_chunks | done | ffd8cd5 |
| DT-9 | 1 | Add SQLite persistence schema with claim-loop indexes | done | 5c9777b |
| DT-10 | 1 | Integrate approved plans, validate implementation, and record orchestration | done | dcf1b81 |
| DT-11 | 1 | Add minimal environment-backed persistence settings | done | 63ecaeb |
| DT-12 | 1 | Add async SQLite connection factory and explicit transaction boundary | done | b152b51 |
| DT-13 | 1 | Implement document, execution, and cache repositories | done | a261f0c |
| DT-14 | 1 | Implement async filesystem storage with contained artifact paths | done | 7790553 |
| DT-15 | 1 | Validate persistence integration and record execution corrections | done | 0758d5e |
| DT-16 | 0 | Add requirements traceability section to ARCHITECTURE.md | done | 5391a80 |
| DT-17 | 2 | Extend ChunkRequest with approved source-side context | done | d8a610c |
| DT-18 | 2 | Implement FakeProvider and validated provider settings | done | d8a610c |
| DT-19 | 2 | Implement model token cost calculator | done | d8a610c |
| DT-20 | 2 | Implement structured OpenAI provider and safe error mapping | done | d8a610c |
| DT-21 | 2 | Verify provider contracts, integration, and Stage 2 delivery | done | d8a610c |
| DT-22 | 3 | Implement threaded PDF extraction, rendering, and overflow fallback | done | 6e52e44 |
| DT-23 | 3 | Implement paragraph-level DOCX extraction and style-preserving rendering | done | 6e52e44 |
| DT-24 | 3 | Implement bounded format resolution and reproducible sample documents | done | 6e52e44 |
| DT-25 | 3 | Verify format integration, opaque metadata, measurements, and delivery | done | 6e52e44 |
| DT-26 | 4 | Add worker settings and retry executor | done | 2e17801 |
| DT-27 | 4 | Implement single-job claim loop and lease heartbeats | done | 2e17801 |
| DT-28 | 4 | Implement atomic translation checkpoints and cost control | done | 2e17801 |
| DT-29 | 4 | Implement cache-driven PDF/DOCX assembly | done | 2e17801 |
| DT-30 | 4 | Wire worker process, verify recovery, review, and deliver | done | 2e17801 |
| DT-31 | 5 | Wire FastAPI factory, dependencies and structured errors | done | 89df2ac |
| DT-32 | 5 | Implement job service, idempotency and retry coordination | done | 89df2ac |
| DT-33 | 5 | Implement document upload service and router with triage stub | done | 89df2ac |
| DT-34 | 5 | Implement jobs, batches, downloads and SSE routers | done | 89df2ac |
| DT-35 | 5 | Implement readiness and metrics; integration review and verification | done | 89df2ac |
| DT-36 | 6 | Add analyzing status and structured triage output | done | df79429 |
| DT-37 | 6 | Implement navigation tools, fake and OpenAI triage adapters | done | df79429 |
| DT-38 | 6 | Implement background triage, degraded fallback and retry coordination | done | df79429 |
| DT-39 | 6 | Wire upload scheduling and enforce job analysis readiness | done | df79429 |
| DT-40 | 6 | Verify triage integration, review, document and deliver | done | df79429 |
| DT-41 | 7 | Add shared-directory settings and path containment | done | efcc377 |
| DT-42 | 7 | Add recent-job query and REST collection route | done | efcc377 |
| DT-43 | 7 | Compose MCP runtime and streamable HTTP lifecycle | done | efcc377 |
| DT-44 | 7 | Implement bounded submission and atomic cross-process triage | done | efcc377 |
| DT-45 | 7 | Add job tools and protocol/worker end-to-end tests | done | efcc377 |
| DT-46 | 7 | Document, review, verify and deliver Stage 7 | done | efcc377 |

| DT-47 | 8 | Scaffold React TypeScript Vite frontend | done | 17b079c |
| DT-48 | 8 | Add typed API client and safe error mapping | done | 17b079c |
| DT-49 | 8 | Expose document readiness through service and REST | done | 17b079c |
| DT-50 | 8 | Implement validated upload and readiness submission | done | 17b079c |
| DT-51 | 8 | Implement batch job SSE retry and download views | done | 17b079c |
| DT-52 | 8 | Implement recent job history and filters | done | 17b079c |
| DT-53 | 8 | Apply local Stark branding and accessible layout | done | 17b079c |
| DT-54 | 8 | Serve frontend with API-safe 404 SPA fallback | done | 17b079c |
| DT-55 | 8 | Document two-terminal frontend workflow | done | 17b079c |
| DT-56 | 8 | Verify Stage 8 integration and acceptance | done | 17b079c |
| DT-57 | 8 | Review and deliver Stage 8 with commit records | done | 17b079c |

| DT-58 | 9 | Deliver non-root multi-stage image and three-process Compose | done | 52c280d |
| DT-59 | 9 | Prove durable restart recovery with Compose chaos script | done | 52c280d |
| DT-60 | 9 | Add stale-chunk worker-aware readiness | done | aa2b9d6 |
| DT-61 | 9 | Implement live quality and cost measurement command | done | 1969f27 |
| DT-62 | 9 | Record real measurements or explicit measurement gaps | done | 8edd4ad |
| DT-63 | 9 | Document operator runbook and offline frontend CI | done | 8edd4ad |
| DT-64 | 9 | Review Stage 9, verify acceptance and deliver commits | done | 8edd4ad |
| DT-65 | 9 | Publish MCP downloads readable by the host from non-root containers | done | d1a4809 |

### Stage 9 execution decomposition (2026-10-03)

DT-58–DT-64 execute the approved Stage 9 plan. Container/chaos delivery,
readiness, and measurement tooling have disjoint delegated ownership. Root
owns integration, documentation, CI, acceptance, independent review and
commits. Existing user changes are preserved and excluded from delivery.
DT-65 fixes a host-readability defect found during real Compose MCP downloads.

Stage 9 is implemented and independently reviewed. Final checks: 493 backend
tests, 66 frontend tests, lint, backend/frontend typecheck and frontend build
passed. Docker build/import/sqlite3/tokenizer checks passed; REST/MCP acceptance
and restart chaos passed again from a clean local clone of runtime commit
`d1a4809`. Chaos recovered 10/10 chunks and 320/320 translations, with no new
attempts for already-done chunks. Real OpenAI sample measurements and explicit
unmeasured limits are recorded in DECISIONS.md. Details and review rulings:
[Stage 9 execution record](docs/plans/2026-10-03-stage-9-execution.md).


### Stage 7 execution decomposition (2026-10-02)

Root owns DT-41 config/path validation and DT-46 docs/integration/delivery.
Delegated DT-42 recent jobs, DT-44 shared triage, DT-43–DT-45 MCP tools and
protocol tests to disjoint owners; a separate reviewer checked the final scope.
The approved plan and user refinements authorize the public additions.
Keep the existing SQLite schema; service-initiated conditional claims accompany
shared advisory locks, allowing cross-process exclusivity and killed-owner
recovery. Per-document upload locking also protects shared ingestion artifacts.
MCP polls with short connection scopes, returns pending within 45 seconds even
under writer contention, and retains background readiness work after timeout.
Use the returned document ID with check_status and resubmit once extracted.

Acceptance: `make test` — 421 passed, 2 live tests deselected; `make lint` — clean
(130 files); `make typecheck` — clean (57 source files). Independent MCP review:
31 tests passed and no remaining findings. Real local streamable HTTP:
four tools discovered; PDF submitted, fake worker completed, translated file
downloaded; recent jobs and containment rejection verified. No live OpenAI calls.
Manual Claude Code with an isolated temporary MCP config returned a client error;
authenticated editor validation remains unverified, with a reproduction recipe
in README. Compose delivery remains Stage 9. Existing user edits are preserved;
only Stage 7 documentation hunks enter delivery. See the execution record.

### Stage 6 execution decomposition (2026-10-02)

- DT-36: orchestrator owns approved core model additions and tests.
- DT-37: delegated adapter implementation and focused offline/live tests.
- DT-38 / DT-39: orchestrator owns core services, internal persistence helpers,
  background composition, routers and integration tests.
- DT-40: independent delegated review, full acceptance checks, docs and commits.

Rulings: the user's execution request approves the plan's public additions.
Keep the existing TriageAgent port returning TranslationPlan; service persistence
converts it to DocumentAnalysisRecord. reasoning contains a brief explanation
of evidence, never a request for private chain of thought. Navigation sees only
seq/source_text. No schema migration; explicit retry recovers a stuck analyzing
record. Use next sequential task IDs. Preserve unrelated user changes in
DECISIONS.md, PROMPTS.md, TEST_TASK.md and docs/roadmap.md. Work in the existing
stage-5-rest-api task branch to preserve the user's workspace. Implementers
own disjoint files, do not commit or spawn agents. One implementation commit
and a separate hash-backfill commit; no amend/rebase/push.

Stage 6 implementation and independent review are complete. Acceptance checks:
`make test` — 367 passed, 2 live tests deselected; `make lint` — clean (113 files
formatted); `make typecheck` — clean (49 source files). The 65 new offline tests
cover strict models, bounded navigation, SDK output/errors/client lifetime,
fake determinism/faults, response-before-analysis ordering, independent WAL
connections, retry/fallback/cancellation, crash recovery, immutable analyses,
atomic publication/enqueue rollback, stale glossary races and duplicate uploads.
Threaded tests ran outside the sandbox. Two existing Pydantic register warnings
remain; no live API calls were made. Independent review also verified a real
Agents SDK loop using an offline mock transport. Review reproduced and resolved
plan replacement/cache inconsistency: analysis freezes after any job exists,
and atomic enqueue checks current analysis terms. Scope and execution rulings
are recorded in the Stage 6 plan; operating notes are in docs/api.md.
Unrelated user changes are preserved and excluded from delivery.

### Stage 5 execution decomposition (2026-10-02)

- **DT-32:** agent owns job service, internal API persistence helpers, retry
  policy and focused tests; worker changes for retry budgets only.
- **DT-33:** agent owns document service, documents router and focused tests.
- **DT-34:** agent owns jobs router, SSE and API integration tests.
- **DT-31 / DT-35:** orchestrator owns dependencies, app factory, catalogued
  errors, health/metrics, integration review, documentation and delivery.

Ruling: preserve approved REST schemas, core records, repository ports and DDL.
SQL stays in persistence; service writes use injected transaction contexts.
Use file-backed temporary SQLite for separate request connections and real WAL.
Use the next sequential task IDs rather than the provisional IDs in the plan.
Work on the stage-5-rest-api branch; preserve existing user changes.
Implementers own disjoint files and do not commit or spawn agents.

Stage 5 implementation and independent review are complete. Acceptance checks:
`make test` — 302 passed, 1 live test deselected; `make lint` — clean (105 files
formatted); `make typecheck` — clean (44 source files). The 29 added tests cover
real PDF/DOCX uploads and FakeProvider output, multi-language idempotency races,
partial enqueue recovery, fresh retry budgets with retained attempt costs after
SQLite restart, download range errors, SSE termination/connection closure,
readiness/metrics, 400-page and extracted-text boundaries, atomic rollback and
cancellation-safe upload cleanup. Full threaded tests ran outside the sandbox;
no live API calls were made. Two existing Pydantic register warnings remain.
Independent review found and verified fixes for missing extraction limits and
an orphaned-file race during cancellation. Operating limits, triage stub and
initial cache-hit metric scope are in `docs/api.md`; execution rulings and the
AI delegation/review log are in the Stage 5 plan. User changes remain excluded.
Delivery uses one implementation commit followed by a task-hash backfill commit.

### Stage 4 execution decomposition (2026-10-02)

- **DT-26 / DT-29:** agent owns settings, executor, assembly, and focused tests.
- **DT-28:** agent owns translation loop and focused tests.
- **DT-27 / DT-30:** agent owns claim loop, entrypoint, integration and resume tests.
- Orchestrator owns internal persistence coordination, shared error catalog,
  semantic cache key, integration review, documentation, checks, and commits.

Existing public ports, domain models, and schema remain the approved contracts.
Internal persistence helpers load chunk links/attempt numbers and serialize reads
with writes on the worker connection. Provider calls and rendering run outside
transactions. Heartbeats continue through assembly; restart releases expired
chunk leases. Task IDs use the next available sequential numbers rather than
the plan's provisional DT-31–DT-35. Existing user changes are preserved.

Stage 4 implementation and independent review are complete. Acceptance checks:
`make test` — 273 passed, 1 live test deselected; `make lint` — clean (86 files
formatted); `make typecheck` — clean (30 source files). The 31 worker tests cover
real PDF/DOCX output, checkpoint rollback, concurrent cost reservations, WAL
writes during provider calls, retries and attempt usage, leases through slow
assembly, restart after closing/reopening SQLite, assembling recovery, safe
render errors, single-job graceful shutdown, and subprocess SIGTERM. Two
additional persistence tests verify serialized worker reads and ownership.
Threaded SQLite/I/O tests ran outside the sandbox; live calls were not run.
The two existing Pydantic `register` warnings remain. Operating instructions
and the bounded lease-loss delay are documented in `docs/worker.md`.
Delivery uses one implementation commit followed by a task-hash backfill commit.

### Stage 3 execution decomposition (2026-10-02)

- **DT-22:** PDF adapter and focused tests; delegated ownership of `pdf.py`
  and PDF unit tests. Reading order, scanned/corrupt files, redaction,
  bounded font shrinking, Unicode, and paginated overflow are reviewed.
- **DT-23:** DOCX adapter and focused tests; delegated ownership of `docx.py`
  and DOCX unit tests. Paragraph styles survive, inline formatting is cleared,
  and untouched tables/headers remain on the original canvas.
- **DT-24:** registry, generator, samples, and registry tests; delegated
  ownership. Resolution reads at most 2048 bytes in a worker thread.
- **DT-25:** orchestrator owns safe format errors, metadata and integration
  tests, independent review, fallback measurements, documentation, full
  acceptance checks, commits, and hash backfills.

Agents own disjoint files, read architecture and installed library source before
implementation, and do not commit. The existing core ports/models are retained.
The approved Stage 3 plan includes DOCX now despite the older stage ordering.

Stage 3 implementation and independent review are complete. Acceptance checks:
`make test` — 238 passed, 1 live test deselected (including 30 format tests);
`make lint` — clean; `make typecheck` — clean (22 source files). Live calls were
not run. Tests requiring actual threads ran outside the tool sandbox without
changing async behavior. The two existing Pydantic `register` warnings remain.
Both the FakeProvider-prefix baseline and synthetic >=30% expansion measured
3/12 fallback blocks and three appended pages on the two-page PDF sample.
Ordinary paragraphs fit; table rows fall back. DOCX covers top-level paragraphs,
leaving table cells/headers/footers unchanged. Limits and measurements are in
DECISIONS.md; ownership, corrections, and review are recorded in the Stage 3
plan and PROMPTS.md. Delivery uses the plan's single implementation commit
option followed by a task-hash backfill commit.

### Stage 2 execution decomposition (2026-10-02)

- **DT-17:** approved context fields and model roundtrip/default tests; orchestrator owns core models.
- **DT-18:** FakeProvider, Settings, environment example, and focused tests; delegated ownership.
- **DT-19:** pricing implementation and focused tests; delegated ownership.
- **DT-20:** OpenAI adapter, offline transport tests and opt-in live contract test; delegated ownership.
- **DT-21:** shared provider error catalog, reusable port test-kit, independent review,
  documentation, full acceptance checks, commits, and hash backfills; orchestrator owns integration.

Agents work in disjoint files. Core context and errors are supplied first;
SDK source inspection precedes implementation. Real API tests remain opt-in.

Stage 2 is implemented and independently reviewed. Acceptance checks:
`make test` — 205 passed, 1 live test deselected; `make lint` — clean;
`make typecheck` — clean (20 source files). Pytest excludes live tests by default;
explicit `-m live` selects the shared OpenAI port contract. Live calls were not
run. Tests requiring real threads ran outside the tool sandbox. The two existing
Pydantic `register` warnings remain. Implementation is delivered as one combined
Stage 2 commit, as allowed by the approved plan, followed by hash backfills.
Review findings and the AI delegation log are in
`docs/plans/2026-10-02-stage-2-llm-provider-execution.md`.

### Stage 1 execution decomposition (2026-10-02)

- **DT-7:** add missing record models and attempt outcomes, forbid extra model
  fields, preserve opaque nested metadata, and add focused model validation tests.
- **DT-8:** return persisted analysis records, carry explicit chunk-block join
  records in aggregate enqueue, and update repository port contract tests.
- **DT-9:** add the approved DDL/package and verify schema loading, indexes,
  record-to-column parity, uniqueness, foreign keys, and cascading deletion.
- **DT-10:** coordinate disjoint file ownership, review all changes, update
  architecture and plan results, run full checks, and backfill task commit hashes.

DT-8 depends on the new record types from DT-7; DT-9 DDL can proceed independently.
Schema/model parity tests run after DT-7 is ready. Each implementation task gets
its own commit with its tests and corresponding approved plan.

### Stage 1 persistence implementation decomposition (2026-10-02)

- **DT-11:** minimal Settings with defaults, environment overrides, and tests.
- **DT-12:** injected async connections, all four PRAGMAs, optional DDL loading,
  and an explicit application/service transaction context for ordinary writes.
- **DT-13:** all three existing repository ports, atomic aggregate enqueue,
  opaque JSON persistence, UTC dates, leases/recovery, attempt accounting, and
  cache insertion/read-back. Depends on DT-12.
- **DT-14:** contained upload/output paths, threaded filesystem I/O, and tests
  for round trips, missing artifacts, traversal, and symlinks.
- **DT-15:** independent integration/recovery/concurrency review, full checks,
  updated plan/log documentation, and task commit-hash backfills.

Foundation, repositories, and filesystem work have separate file ownership and
run in parallel. Existing DT-7–DT-9 remain completed; new work uses new IDs.
The execution plan's in-memory WAL test and prefix-based path check are corrected
without changing approved core models, ports, or DDL.

Implementation and independent review are complete. Acceptance checks:
`make test` — 145 passed; `make lint` — clean; `make typecheck` — clean
(17 source files). Real threaded I/O tests ran outside the tool sandbox.
The existing two Pydantic warnings for approved `register` fields remain.
See the stage plan and PROMPTS.md for review findings and operating assumptions.

### Stage 8 execution decomposition (completed 2026-10-03)

DT-47–DT-57 implement the approved frontend plan with scoped delegation and
independent specification/quality reviews. Root owns integration, browser
acceptance, docs, and delivery. User refinements authorize the document status
contract, MIME checks, Strict Mode cleanup, and custom 404 SPA handler.

All reviews passed. Final review's aggregate HTTP/1.1 SSE starvation finding
was corrected with four shared streams and cancellable GET polling for excess
active jobs; scoped re-review confirmed closure without new major regressions.
The existing checkout remains on `stage-8-frontend`, preserving unrelated user
files. No public schema changes beyond the approved plan; Compose stays Stage 9.

Acceptance: 476 backend tests and 66 frontend tests passed; lint, backend and
frontend typecheck, production build, 16 real-browser PDF/DOCX checks, and five
eight-job HTTP/1.1 capacity checks passed. Fake providers only. Detailed decisions
and evidence: [Stage 8 execution record](docs/plans/2026-10-02-stage-8-frontend-execution.md).

# TASKS.md — Backlog & Task Log

Ticket source of truth for commit messages (see
[CONTRIBUTING.md](./CONTRIBUTING.md)). Prefix: **DT**. Numbers are
sequential and never reused, even if a task is cancelled.

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
3. Format adapter: PDF (extractor + renderer)
4. Worker: leases, claim loop, bounded-parallel executor, retry/backoff
5. REST API + SSE
6. Format adapter: DOCX (proves the format port)
7. Triage agent (openai-agents SDK) + degraded fallback
8. MCP server
9. Frontend (React, Stark branding)
10. Chaos script + measurements for DECISIONS.md

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
| DT-11 | 1 | Add minimal environment-backed persistence settings | done | pending commit |
| DT-12 | 1 | Add async SQLite connection factory and explicit transaction boundary | in-progress | |
| DT-13 | 1 | Implement document, execution, and cache repositories | in-progress | |
| DT-14 | 1 | Implement async filesystem storage with contained artifact paths | in-progress | |
| DT-15 | 1 | Validate persistence integration and record execution corrections | in-progress | |

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

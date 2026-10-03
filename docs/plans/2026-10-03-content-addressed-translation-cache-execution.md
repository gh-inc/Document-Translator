# DT-91 execution record

Plan: [content-addressed translation cache](2026-10-03-content-addressed-translation-cache.md).

## Decomposition and ownership

- Persistence agent: Tasks 2–3, models/ports, SQLite counters, retry SQL and metrics snapshot.
- Pipeline agent: Tasks 1, 4–5 and 7, semantic key, worker, job service and regression tests.
- Presentation agent: Tasks 6 (HTTP export), 8–9, REST/SSE/MCP and frontend.
- Root: Task 10 documentation, Task 11 integration, independent reviews and scoped commits.

## Plan review and rulings

The execution request approves the schema and REST/SSE/MCP additions described in the plan, and supersedes its instruction to leave work uncommitted. Work uses a dedicated branch in the existing checkout, preserving pre-existing user edits.

| Tasks | Shared interface | Resolution |
|---|---|---|
| 1, 4, 5 | Semantic translation key | Pipeline owns all consumers and the new core module. |
| 2, 3, 4, 6, 8 | Job counters and repository ports | Persistence owns shared models/ports; others consume them. |
| 5, 6 | Persistence API | Persistence owns retry SQL and metric snapshot. |
| 6, 8 | HTTP tests | Presentation owns HTTP tests and exporter. |
| 7 | Three-block proof versus 99% assertion | Use 2/3 = 67% for the edited three-block case; add a separate 99/100 proof if needed. |
| 2 | Legacy index before migration | Ensure old-column index cannot cause schema initialization failure before migration. |
| 4, 10 | Counter exactly-once claim | Counts measure durable lookup observations, including retries; do not claim retries cannot count repeated lookups. |

All other tasks are internally consistent. No separate spec is referenced; plan invariants are the authority. Neighbour context is deliberately excluded from cache identity, as approved. No live calls or push.

## Review and verification

Independent specification and quality review found no P1/P2 issues. Scoped
re-review covers concurrent legacy initialization and the WAL activation retry.
A concurrent rollback-journal upgrade exposed immediate SQLite locking despite
busy_timeout; WAL initialization now retries asynchronously during a 20-second
window, with a final SQLite wait potentially extending that duration. This is
an internal startup fix, not a new public contract.

Migration tests cover legacy index present/absent, concurrent initializers,
zero counters, preserved existing jobs, dropped old cache rows, and reopening
without waiting for an unrelated writer. Cache rows now outlive document
cascades because source hashes have no document foreign key.

Presentation checks: 33 backend tests passed; 71 frontend tests and production
build passed. Pipeline checks: 71 cache-key/job-service/worker tests passed.
DOCX E2E proof: cold first job `(0 hits, 3 misses)`; edited document `(2, 1)`
and exactly one provider block; changed domain `(0, 3)` and three provider
blocks. Rendered paragraphs match FakeProvider output in all three artifacts.
The edited job shows 67% cached. Temporarily restoring lookup by block ID
made the second document resend all three paragraphs and fail the regression;
the correct source-hash lookup was restored and passed.

Initial full suite ran during implementation and found four regressions;
old cascade and Markdown accounting expectations were corrected, and temporary
migration diagnostics were removed. The final full suite passed: 597 backend tests, 2 live tests deselected.
Lint passed (165 files), backend mypy passed (60 source files), and frontend
typecheck passed. The last full-suite failure was one stale model fixture; it
was corrected, its 44 model tests passed, and the full suite was rerun green.

No live provider comparison was run. `bash -n scripts/chaos-restart.sh` passes;
its cache joins were updated for source hashes and duplicate paragraph coverage.
Container chaos was not rerun for this ticket.

## Delivery

Implementation: `3f9fff4` — `DT-91: feat(cache): reuse plan-scoped translations by source hash`.
Task-hash backfill and token report are delivered in a separate documentation
commit without amending or rewriting history. The branch is
`content-addressed-translation-cache`. Pre-existing user edits remain unstaged.

## Token accounting

Measured from Codex session `token_count.info.total_token_usage` at the report
checkpoint after implementation commit and before this accounting commit,
2026-10-03T18:58:15.857562+00:00. Includes root and all four delegated
agent sessions, including resumed turns. Root baseline before this task is zero.

| Agent | Input | Cached input (included) | Output |
|---|---:|---:|---:|
| `/root/persistence` | 6,303,557 | 6,200,620 | 16,322 |
| `/root/review` | 1,789,420 | 1,686,426 | 3,583 |
| `/root/pipeline` | 8,337,498 | 8,260,287 | 16,293 |
| `/root/presentation` | 2,298,713 | 2,246,278 | 9,630 |
| `/root` | 9,646,346 | 9,521,309 | 15,205 |
| **Total** | **28,375,534** | **27,914,920** | **61,033** |

Uncached input: 460,614. Output already includes
11,960 reasoning tokens; they are not added twice. Input
counts accumulate every model request's context, so cached prefixes are counted
again on subsequent turns; this is token usage, not unique text or a billing
estimate. The final accounting commit and final response occur after this
checkpoint and are excluded; a self-referential exact final-response total
cannot be recorded before that response exists. No provider translation calls
were made during verification.

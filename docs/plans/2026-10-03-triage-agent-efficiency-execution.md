# DT-93 — triage efficiency execution (2026-10-03)

Source: [implementation plan](2026-10-03-triage-agent-efficiency.md).

## Decomposition and ownership

- triage_backend: Tasks 1–3, service retry/accounting, terminal error,
  installed SDK exception handling, validated limits and runtime wiring/tests.
- cost_api: Task 4, document response and core service reads, cumulative cost
  at every response site and API/service regression tests.
- cost_ui: Task 5, strict parsing, once-per-batch rendering and navigation/failure tests.
- root: Tasks 6–7, documentation, integration checks, independent review,
  task tracking, scoped commit and telemetry accounting.

## Rulings

1. Use DT-93 because DT-92 already belongs to the delivered benchmark matrix.
   Reusing it would misattribute history; the implementation scope is unchanged.
2. Stop ProviderError when `not retryable or terminal`. The plan's sample
   checks only terminal, and its decision bullet erroneously says retryable
   failures stop. Both contradict the primary requirement. Retryable failures
   and bare exceptions keep three attempts; regression tests pin the policy.
3. The owner's explicit request to execute the supplied plan authorizes its
   concrete public response field and overrides the plan's older no-commit note.
   No other contract is introduced.
4. Fresh upload with no analysis reports zero; duplicate upload and retry
   responses report existing cumulative expense. Returning literal zero for
   those would contradict cumulative accounting. Only one repository read is
   added per document response; no job polling payload grows.
5. Work in the shared checkout with disjoint ownership to preserve existing
   uncommitted user edits. Delivery stages explicit task paths only; unrelated
   drafts and existing PROMPTS edits remain outside the commit. The supplied
   task source plan is included after formatting its Python snippets.

## Evidence

Installed `agents/run.py` and `run_internal/prompt_cache_key.py` confirm default
per-run affinity generation for supported model adapters. No provider caching
API changes or new dependencies are needed.

No invalid-key live request is made. Auth fail-fast claims rely on deterministic
regression tests, not measured live latency or billing savings.

## Review and observed execution

Independent reviewer found no actionable defects in all three implementation
packages and the full cross-layer changes. Frontend tests cover malformed money,
failed document reads, once-per-batch rendering and stale navigation responses.
Backend tests pin terminal accounting, unchanged catalog retryability, configured
limits/guard and cumulative costs on GET, duplicate upload and retry.

ASGI smoke with real temporary SQLite and FakeTriageAgent logged one
`triage_cost_recorded` event: input 173, cached input 61, output 29, requests 3,
`cost_usd_delta=0.000038775`, then `triage_completed` with `triage_status=ok`.
Fresh upload returned `analysis_cost_usd=0.0`; readiness GET returned
`analysis_cost_usd=0.000038775`, status extracted. No actual provider spending.
At four decimals the UI rounds this small sample to $0.0000, consistent with
existing job cost formatting; the UI regression uses $0.0042.

Initial full-suite runs overlapped implementation and loaded old API assertions.
One new usage assertion also incorrectly priced cached input; the implementer
corrected its expected total from $0.000003825 to $0.000003375. The final suite
below runs after all edits. The first smoke used system Python without project
dependencies; it was rerun using the project virtual environment. SQLite checks
needed sandbox escalation because aiosqlite stalled inside the sandbox.
The supplied source plan's Python snippets were formatted for global make lint;
its wording remains unchanged, with corrections recorded above.

## Verification and delivery

- `make test`: 633 passed, 2 live tests deselected, 2 existing Pydantic warnings,
  79.93 seconds; exit 0, final run after all implementation edits.
- `make lint`: checks passed; 172 files formatted.
- `make typecheck`: no issues in 61 source files.
- `npm --prefix frontend test`: 8 files, 78 tests passed.
- `npm --prefix frontend run build`: TypeScript and production Vite build passed.
- ASGI smoke and staged `git diff --check`: passed.

All seven plan tasks completed. Independent initial and scoped final reviews
found no blocking issues. Branch `triage-agent-efficiency` preserves the checkout;
no merge, push or live calls. Implementation commit and token snapshot follow.


## AI usage log

Implementation delegation used isolated task context and shared disjoint file
ownership. Plan retry pseudocode and cumulative-upload timing assumptions were
rejected as described in Rulings. Independent review and a scoped follow-up found
no blocking findings; the pricing correction was explicitly recalculated.
The delivery stages only the new DT-93 PROMPTS entry using a selective index
patch, preserving earlier user edits in the working tree.

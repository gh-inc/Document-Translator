# Triage cost observability — execution record

Plan: [approved implementation](2026-10-03-triage-cost-observability.md).
Tickets: DT-80–DT-85; DT-79 remains reserved for separate DOCX table work.
Branch: `triage-cost-observability`, based on `pdf-glyph-resilience`.

## Decomposition and ownership

| Tasks | Owner | Responsibility |
| --- | --- | --- |
| 1, shared contracts | Root | DTO, record fields and ports |
| 1, 4 | Persistence agent | DDL, startup upgrade, row mapping, atomic republish |
| 2, 3 | LLM agent | Cached pricing, SDK usage on success/failure, deterministic fake |
| 4, 5 | Service/metrics agent | Retry accounting, runtime wiring, durable metric snapshots |
| 6, integration | Root | Documentation, independent review, acceptance, scoped commits |

Agents share disjoint file ownership and do not commit. Root stages only this
task's changes. Existing edits to PROMPTS.md, assessment roadmap, glyph execution
record, OVERVIEW.md and the DOCX-table plan are preserved and excluded.

## Implementation rulings

- Record columns append after `created_at`, preserving exact field/column parity
  for both fresh databases and additive upgrades. Startup upgrades existing rows
  with zeros; historical usage cannot be recovered.
- Repository `save_analysis` accepts current-plan usage and complete cumulative
  totals. The service reads previous totals before degraded deletion and carries
  them into the new row in the same transaction. Without explicit totals, the
  repository adds current deltas to existing cumulative values.
- Current-plan usage describes the final successful attempt; cumulative values
  include known failed attempts and automatic retries. A heuristic fallback has
  no provider-produced plan, but retains known failed usage in cumulative totals.
- SDK failure usage must come from a retained `RunContextWrapper`, not an
  unavailable `RunResult.usage`. Missing usage remains zero and is explicitly
  unknown, not evidence that the invocation was free.
- Official supported-model cached prices are 50% of normal input rates. The
  plan's approximate tenth-rate explanation is corrected; pricing uses the
  explicit model snapshot.
- Historical Stage 9 triage-inclusive numbers cannot be reconstructed. Replace
  the undocumented invoice-total promise with the computable durable formula,
  preserving historical bulk observations and residual unknown-usage limits.
  No new live calls are required or performed.
- The service deadline is 65 seconds, exceeding the SDK adapter’s 60 seconds;
  the SDK can map its timeout to usage-bearing ProviderError before service
  cancellation. True external cancellation still propagates. Cost logs emit
  after commit and are best effort across the commit-to-log crash window.
- Metrics remain document-level and independent of job joins. Scrapes read
  cumulative columns using a new registry, preserving restart/retry behavior.

## Verification and review

Initial integration check after the DTO fields were added: 525 passed,
1 expected schema-parity failure while the delegated DDL edit was pending,
2 live tests deselected. This was a partial implementation check, not a clean
pre-change baseline.

Focused verification (outside sandbox where worker-thread I/O initially hung):

- Persistence: 56 passed, including a true legacy-row upgrade and concurrent
  startup, parity, overwrite and delete/republish accounting.
- Adapter/pricing: 68 passed, one live test deselected. Aggregate SDK usage,
  known failure usage, nonzero timeout usage and cancellation are covered.
- Service/metrics: 29 passed. Failed+successful attempts accumulate to exact
  durable totals; three-language attribution, repeated scrapes and reopened
  databases are covered.
- Root models/provider errors: 56 passed.

Independent final review found one important issue: equal service/SDK timeout
limits could cancel the adapter before it attached known usage. The 65/60-second
limits resolve it; the adapter timeout regression now asserts nonzero usage,
and existing cancellation/recovery tests remain intact. Documentation review
also removed claims about unexported metrics and qualified post-commit logs.
Re-review: no outstanding critical or important findings; specification and
code quality approved. No live provider calls were made.

The first full integration run on a changing tree had stale fixture expectations;
those numeric expectations were corrected in the focused service run. A second
full run exposed a genuine migration regression: every connection took a write
lock even when all accounting columns already existed, blocking MCP's bounded
read/claim flow behind an independent writer. The fix checks the schema without
a write lock first and only locks/rechecks when columns are missing. This keeps
concurrent startup upgrades safe while preserving normal read behavior.
Migration correction: 66 persistence/MCP tests passed, including the new
bounded factory-open-under-writer regression and existing MCP recovery test.
Independent scoped re-review found no remaining issues; missing-column
upgrades still recheck under the write lock, while existing schemas use a
read-only fast path. Final acceptance on the completed code:

- `UV_CACHE_DIR=/tmp/document-translator-uv-cache rtk make test`: 550 passed,
  2 live tests deselected, 70.65 seconds; only the two existing Pydantic
  `register` warnings remain.
- `make lint`: clean, 153 files formatted.
- `make typecheck`: clean, 59 source files.
- `git diff --check`: clean.

Root commits the implementation under DT-85 and then backfills delivery hashes
in a separate documentation commit. No amend, rebase, merge, push or real
provider invocation.

## Token accounting

Read only `token_count` metadata from this Codex session and its descendant
agent rollouts. Report cumulative input/output totals, including cached input,
at the final accounting snapshot. The final response itself is not included.

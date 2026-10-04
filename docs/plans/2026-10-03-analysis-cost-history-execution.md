# DT-103: analysis cost history execution

The owner requested orchestration, delegated implementation, verification and
commits on 2026-10-04. Existing commits `42fd33c`, `26c64b4` and `30735de`
already implemented the main feature. This execution audited and completed it.

## Ownership and review

- Backend agent: batch response lookup, finite/strict response schema, actual
  SQLite statement-count regression and API/schema tests.
- Frontend agent: strict REST parser, singular accessible label, preservation of
  known analysis cost after Retry, and frontend regressions.
- Root: backlog, documentation, full checks, Compose/browser acceptance and delivery.
- Independent reviewer: specification and quality audit of existing feature and
  completion diff; no functional blockers. Two documentation findings were fixed
  and scoped re-review approved the final result.

## Final behavior

History shows cumulative document triage cost once per visible document and labels
how many visible translations share it. Zero is hidden. Status filtering changes
the shared count, while Retry preserves the known cost. Bulk job costs stay separate.
List/create/batch responses use one bound-parameter lookup for distinct documents;
an empty list performs none. Single-job GET/retry defaults and SSE remain as specified.
`JobRecord`, database schema and dependencies are unchanged.

## Rulings

- Use DT-103: DT-102 was occupied, and prior feature commits already used DT-103.
  Cost if wrong: task-log relabelling only.
- Preserve the existing checkout and prior feature commits; audit their gaps.
  Cost if wrong: branch placement would need adjustment by the owner.
- Validate analysis cost in REST `parseJob`, not shared `parseProgress`: SSE does
  not contain that field. Cost if wrong: REST/SSE contract work would be needed.
- Keep the branch after the requested local commits; no merge or push was requested.

## Acceptance

- `make test`: 706 passed, 2 live tests deselected; six existing Pydantic warnings.
- `make lint`: clean, 181 formatted files.
- `make typecheck`: clean, 61 source files.
- Frontend tests: 88 passed; TypeScript/production build passed.
- `docker compose up -d --build`: completed and web became healthy.
- Chrome/Playwright against rebuilt Compose: ten existing jobs rendered; analysis
  lines matched first visible document costs. Controlled responses checked once-only
  rendering, zero hiding, filtered shared count and singular accessible name.
  Screenshot: `/tmp/dt103-history-browser.png`. No provider calls were made by checks.

Sandbox SQLite fixture startup and Chrome launch were blocked; checks were rerun
outside the sandbox. The initial browser check incorrectly assumed no alerts on
historical jobs, which may legitimately contain translation errors; the corrected
check verifies that every returned job renders a card.

## Agent token accounting

Measured from local session `token_count` records, root request delta plus three
child sessions. Input includes cached input and repeated context; these are token
counts, not unique text or billing totals. A final checkpoint is recorded below;
later commit/report turns fall outside that checkpoint.

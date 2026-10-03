# Markdown format execution

Plan: docs/plans/2026-10-03-markdown-format.md

DT-86 adapter/fixture, DT-87 bypass/completion, DT-88 registry/composition/UI,
DT-89 integration/docs/review/delivery. Agents own disjoint files; root owns
TASKS, architecture, decisions and delivery. User request approves the plan's
RenderResult field and Markdown upload/download changes. No database change.
Existing user edits are preserved and excluded from commits.

Ruling: use a new feature branch in the existing checkout to preserve the
untracked approved plan and current user work; do not move or discard it.
Ruling: reconstruct skip IDs from persisted blocks inside the Markdown adapter
at enqueue through an injected callback. The extractor's mutable attribute alone
cannot survive separate requests, MCP processes or restarts. Core never reads
format_metadata. Classification must be stateless per call.
Ruling: register Markdown in MCP and worker too; the plan's six-touchpoint audit
missed these composition roots and browser MIME validation.
Ruling: commit locally as requested; no merge/push menu is needed.

Ruling: filter empty source blocks from bulk source-neighbor context as well;
otherwise geometry-only cells add newlines to prompts and change token usage.
The cost invariant is verified for bulk translation with FakeProvider; real
triage spend varies with agent navigation and is not claimed invariant.

Ruling: the checked-in Platon Markdown fixture has 8 empty table cells, not
the 20 claimed by the input plan. Verify the real fixture and add synthetic
20-empty-cell coverage without modifying the user fixture.

Ruling: preserve the existing zero-block triage rejection policy. Code-only
or blank Markdown fails analysis with corrupt_file; code passthrough applies
within a document with extracted text or table cells. This limit is documented.
Ruling: restrict table extraction to the planned outer-pipe rows; ordinary
prose containing a pipe must remain a paragraph. Independent review caught this.

## Progress

Implementation and independent specification/code review are complete.

- DT-86: Markdown extractor/renderer, sample and structure/adversarial tests.
- DT-87: optional skip IDs, restart-safe adapter resolver, renderer-satisfied
  structural cells and filtered bulk source-neighbor context.
- DT-88: bounded text detection, REST/MCP/worker wiring, browser MIME checks,
  download naming and fake-worker REST/MCP acceptance.
- DT-89: integration review, documented limits, verification and local delivery.

Root integration review fixed metadata delimiters, duplicated whitespace-only
padding, missing-translation fallback, hostile line breaks and trailing
backslash escaping. Independent reviewer caught prose-pipe false positives;
outer-pipe recognition and regression coverage close that finding. Review also
requested explicit bulk-only cost and table grammar limits, now recorded.
No remaining review findings.

Final verification on the completed implementation:

- `make test`: 576 passed, 2 live tests deselected (60.33 s).
- `make lint`: clean; 160 files formatted.
- `make typecheck`: clean; 60 source files.
- Frontend tests: 67 passed; typecheck and production build passed.
- Focused registry/REST/MCP acceptance: 44 passed, fake providers only.
- `git diff --check`: clean.

Threaded I/O checks initially stalled inside the sandbox; final backend tests
ran outside it. Earlier in-progress runs caught stale fixture expectations;
the completed tree passes. No live provider calls, dependencies or database
schema changes. Two existing Pydantic register-shadowing warnings remain.

Delivery uses one implementation commit plus a separate TASKS hash-backfill
commit, without amending, rebasing, pushing or including unrelated user edits.

Implementation commit: `8f52460`. Ticket hashes are backfilled separately.

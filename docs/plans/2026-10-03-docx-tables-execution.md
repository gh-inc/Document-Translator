# DOCX tables execution — 2026-10-03

## Authorization and decomposition

The user explicitly requested planning, delegation, implementation, verification
and commits. This supersedes the source design's historical design-only note.
No REST, MCP, domain-model or database contracts change. The adapter alone owns
all locator metadata, consistent with Architecture's “Layering & the Document IR”
and “Pipeline” sections. Existing feature branch and unrelated edits are preserved.

| Ticket | Owner | Responsibility |
|---|---|---|
| DT-94 | `/root/adapter` | DOCX extraction/rendering and installed-library inspection |
| DT-95 | `/root/tests` | DOCX regression tests, sample-test updates and real sample round trips |
| DT-96 | `/root` and independent reviewer | Integration, docs, checks, delivery |

## Implementation rulings

- Legacy missing-container locators use `document.paragraphs[paragraph_index]`;
  new body locators use XML body child positions. Confusing these would target
  the wrong paragraph after a table.
- Deduplicate actual XML cells across an entire table, covering horizontal and
  vertical merges. Keep element references rather than ephemeral integer IDs.
- Nested tables, headers and footers remain unchanged; cell/body inline formatting
  is flattened, while paragraph properties and table XML remain on the canvas.
- Existing persisted extractions are not migrated. Byte-identical uploads reuse
  old blocks. The backward-compatible renderer supports those jobs, but tables
  require a newly extracted document. No ingestion/cache policy changes are made.

## Verification

Direct adapter checks confirmed both Platon DOCX samples contain 44 nonempty
paragraphs (16 body, 28 table) and round-trip all 44 replacements while retaining
styles and table count. The committed sample regression passes the complex sample
through `FakeProvider`; no live calls are made.

Initial focused checks: 44 passed; final focused suite across three files: 49 passed. Initial full suite: 630 passed, 3 failed,
2 live tests deselected. The failures were historical sample assertions requiring
unchanged table text or searching only body paragraphs for all translations.
The tests owner updated these to verify translated body/cell content, and added
explicit cell inline-format flattening coverage. A scoped independent re-review
confirmed that fix wave without findings.

Independent implementation review found no must-fix issues. Its design wording
notes (legacy index semantics, merged-cell paragraph granularity) were corrected.
Final verification:

- `make test`: **672 passed**, 2 live tests deselected, 2 existing Pydantic
  `register` shadowing warnings (96.84 seconds).
- `make lint`: Ruff checks and formatting clean, 174 files.
- `make typecheck`: clean, 61 source files.
- Scoped DOCX/sample tests: **49 passed**.
- `git diff --check`: clean.

Sandbox threaded pytest
stalled and was interrupted; verification runs outside the sandbox.

## Delivery and token accounting

Implementation commit: `3d0dd66` (`DT-94: fix(docx): translate top-level table
paragraphs in reading order`). Task hash backfill and token accounting are
delivered in a separate documentation commit, without rewriting history.
Unrelated pre-existing edits are excluded; no push is requested.


Token accounting measured from Codex session
`token_count.info.total_token_usage`, summing root and all three delegated
sessions, including resumed turns. Root session baseline before this task is
zero. This checkpoint follows the implementation commit and precedes the
accounting commit/final response; those later requests are excluded.

| Agent | Input | Cached input (included) | Output | Last usage checkpoint (UTC) |
|---|---:|---:|---:|---|
| `/root` | 4,319,559 | 4,226,229 | 11,094 | 2026-10-03T19:38:58.862Z |
| `/root/adapter` | 628,856 | 573,710 | 4,581 | 2026-10-03T19:34:57.809Z |
| `/root/review` | 2,136,903 | 2,058,372 | 4,094 | 2026-10-03T19:37:39.189Z |
| `/root/tests` | 1,796,989 | 1,750,953 | 11,520 | 2026-10-03T19:37:32.663Z |
| **Total** | **8,882,307** | **8,609,264** | **31,289** | — |

Uncached input: 273,043. Output includes
2,507 reasoning tokens; they are not counted twice.
Input accumulates contexts across requests and includes cache hits; it is not
a count of unique text or a billing estimate. No live translation calls were
made for this task. Exact final-response totals cannot be recorded before the
final response exists.

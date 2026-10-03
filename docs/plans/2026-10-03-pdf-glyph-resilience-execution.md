# PDF glyph resilience execution — 2026-10-03

Implements [the approved plan](2026-10-03-pdf-glyph-resilience.md), tasks DT-76,
DT-77 and DT-78. DT-79 (DOCX tables) is outside this change.

## Ownership and decisions

Three implementers owned disjoint areas: PDF adapter and focused tests;
upload service, REST/MCP call sites and warning tests; worker assembly,
DOCX adapter and their tests. The orchestrator supplied shared models/port,
updated operating documentation, integrated checks and owns delivery.
An independent reviewer checks the combined diff before committing.

The execution request approves the plan's public model/port/response changes.
Work stays in the shared checkout on `pdf-glyph-resilience`; existing edits in
PROMPTS.md, the roadmap and other unrelated files are preserved and excluded.
The plan and all six Platon regression fixture files are part of delivery.

Plan corrections:

- U+FE0F is category Mn, so Cc/Cf filtering alone cannot satisfy the plan's
  test. The adapter also removes Unicode variation selectors FE00–FE0F and
  E0100–E01EF. No glyph mappings or transliteration tables were introduced.
  This loses presentation variation in the bundled fallback font.
- Malformed metadata may carry document text. Diagnostics log only validated
  numeric geometry and fixed reasons, never arbitrary page/bbox values,
  exception messages or tracebacks. Less detail is available for malformed data.
- Duplicate uploads regenerate warnings by re-extracting the stored original
  through the existing format port. This preserves format ownership and avoids
  schema changes, at the cost of extra extraction CPU/I/O on repeated uploads.
- Upload warnings cover removed control/format/variation artifacts as well as
  unsupported visible glyphs, making extraction problems visible without rejection.

## Result

Renderers return strict `RenderResult` values with path, degraded block IDs and
fallback block/page counts. PDF filtering happens before the redaction list is
built, preserving the original canvas for a degraded block. DOCX returns empty
degradation. Assembly chooses final status after rendering and reports partial
completion when degradation exists, even with complete translation cache.

`DocumentIR.warnings` travels through a named strict `UploadResult` into REST
upload responses. Warnings are transient: readiness and retry responses default
to an empty list. No database columns or persistence models changed. Worker and
format diagnostics report stages and safe structural data only.

## Verification

Final `make test`: 526 passed, 2 live tests deselected, with the two existing
Pydantic `register` shadow warnings. `make lint`: clean (151 files).
`make typecheck`: clean (59 source files). Focused owner verification: 22 PDF
tests, 19 assembly/DOCX/sample tests, 3 claim-loop tests and 62 upload/API/MCP
tests passed. Independent source review found no outstanding important findings;
its attempted sandbox test run hung and was stopped, so full acceptance uses
the orchestrator's successful run outside the sandbox. `git diff --check` passed.
No live provider calls, history rewrite, merge or push are part of this task.

A fixture test found an additional edge case: a supported block's bbox can
intersect a degraded block. Excluding the degraded block from `render_items`
alone still lets the neighboring redaction erase its original glyphs. The PDF
adapter subtracts protected degraded rectangles from redaction regions before
modifying the canvas. The ordering remains render-items → redaction → insertion.
This favors source retention; overlapping supported translation can still leave
crowded layout, and PyMuPDF may shift retained source spans slightly when
applying neighboring redactions. All original glyphs survive; pixel-identical
layout is outside the documented PDF fidelity limits.

  Token usage:         124K total  (109K input + 14.6K output)
  Context window:      61% left (109K used / 258K)

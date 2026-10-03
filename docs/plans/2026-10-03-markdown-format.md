# Markdown format adapter — implementation plan

> Execution clarifications (2026-10-03): the checked-in Platon fixture has
> 8 empty cells; a synthetic 11×3 table verifies the planned 20-empty-cell case.
> REST, MCP and worker each require registration (eight wiring touchpoints).
> The token/cost invariant applies to bulk translation; triage still navigates
> the full IR and its live spend may vary. The inherited zero-block policy
> rejects code-only/blank Markdown during triage. See the
> [execution record](2026-10-03-markdown-format-execution.md) for rulings and evidence.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Origin:** a third format, added specifically as evidence that the format
boundary is real and not accidental. `ARCHITECTURE.md` currently claims "adding
a format in 10 minutes = one module implementing both ports + one registry
entry". This plan implements the format and then corrects that claim.

**Approved decisions (user):**

1. **Markdown tables are in scope for v1.** Cell-level extraction, with row
   delimiters and padding stored in `format_metadata`, so a model that
   hallucinates `|` cannot corrupt table structure.
2. **Detection: option (c)** — a text branch in the registry with UTF-8
   validation instead of a magic signature.
3. **Translate** headings, paragraphs, list items, block quotes, and table cells.
   Fenced code is passthrough.
4. **Fixture:** create `samples/sample_en.md`.
5. **Do not refactor the registry now.** Record the real touchpoint list in
   `ARCHITECTURE.md` instead of over-promising.

---

## Audit: what "add a format" actually costs

Verified across the repository — the format adapter is **one** of six places,
not one of two:

| # | Touchpoint | Location |
|---|---|---|
| 1 | `_SUPPORTED_FORMATS` allow-list | `app/core/services/document_service.py:22` |
| 2 | `_MAGIC_BY_FORMAT` signature map | `app/adapters/formats/registry.py:11` |
| 3 | `registry.register(...)` wiring | `app/api/dependencies.py:91-92` (mirrored in tests) |
| 4 | Upload widget `accept` list | `frontend/src/features/upload/UploadForm.tsx:66` |
| 5 | Download extension inference | `frontend/src/features/jobs/JobCard.tsx:46,49` |
| 6 | The adapter module itself | new `app/adapters/formats/markdown.py` |

`ARCHITECTURE.md` gets this corrected table instead of the "10 minutes" claim.

---

## Findings from the real fixture

`samples/platon-complex.md` contains a table that shapes the design:

- **Separator row** `| :--- | :--- | :--- |` on line 18 — structure, never
  content. It must be passthrough; translating it destroys the table.
- **11 data rows × 3 columns = 33 cells, of which 20 are empty** (`| | **Критон** | …`).
  This is the visual form of a merged cell.

**Consequence — empty cells must produce blocks.** A cell whose `source_text` is
empty still carries `prefix` and `suffix`, because the row is reassembled from
per-cell metadata. Skipping empty cells would emit fewer `|` delimiters than the
row needs and shear the table. This is the single most important correctness
rule in this adapter.

**But an empty cell must never enter the translation pipeline.** Feeding a
zero-token block to the chunker produces a degenerate chunk, and storing an
empty `translated_text` in the cache collides with
`UNIQUE(translation_key, block_id)` under `INSERT OR IGNORE` — a repeat run would
read back an empty string as if it were a real translation. Resolution: pipeline
bypass, below.

---

## Design: cell-level extraction

Row delimiters and padding live in `format_metadata`, never in `source_text`.
The renderer reconstructs structure from metadata alone. The model therefore
cannot break the table even if it tries.

`format_metadata` per cell — extended beyond the user's sketch with the locators
the renderer needs:

```python
{
    "kind": "table_cell",
    "line": 19,
    "column": 1,
    "columns": 3,
    "prefix": "| ",  # "| " in column 0, " " elsewhere
    "suffix": " |",  # " |" in the last column only
}
```

`prefix`/`suffix` alone are insufficient: without `line`, `column`, and
`columns` the renderer cannot group cells back into rows or know which cell is
last, so it cannot emit the trailing delimiter.

## Design: other block kinds

```python
{"kind": "heading", "line": 5, "prefix": "## ", "level": 2}
{"kind": "list_item", "line": 7, "prefix": "- ", "marker": "-"}
{"kind": "quote", "line": 33, "prefix": "> "}
{"kind": "paragraph", "line": 35, "prefix": ""}
```

The structural prefix is never part of `source_text`, so it survives
translation untouched. Inline emphasis (`**bold**`) stays inside `source_text`
and is translated as ordinary prose; the renderer does not attempt to preserve
inline styling, matching the DOCX decision.

---

## Task 1 — `app/adapters/formats/markdown.py`

**Files:** create `app/adapters/formats/markdown.py`.

`MarkdownExtractor` and `MarkdownRenderer`, both `async`, with all file work in
`asyncio.to_thread` (AGENTS.md rule 9).

Extractor, line by line:

| Input | Handling |
|---|---|
| fenced block ` ``` ` … ` ``` ` | passthrough, no blocks |
| `\| :--- \| :--- \|` separator row | passthrough, no blocks |
| `\| … \|` table row | one block per cell |
| `#{1,6} ` heading | `kind: heading` |
| `- ` / `* ` / `+ ` list item | `kind: list_item` |
| `> ` block quote | `kind: quote` |
| otherwise | `kind: paragraph` |
| blank line | passthrough, excluded from `seq` |

The extractor additionally exposes `self.skip_block_ids: set[str]` — every block
ID that exists for geometry only and must never be translated.

Parsing rules:

- Split cells on `|`, honouring escaped `\|` inside a cell.
- Drop the leading and trailing empty fragments produced by the outer pipes.
- Preserve interior empty cells — they are the merged-cell case.
- Cell text is stripped for `source_text`; the surrounding padding is
  reconstructed into `prefix` / `suffix`.
- An empty cell yields a block with empty `source_text`, non-empty `prefix` and
  `suffix`, and its ID added to `skip_block_ids`.

Renderer walks the original file and rebuilds it:

- passthrough lines are copied verbatim;
- a translated block replaces the text of its own line;
- table rows are rebuilt from ordered cell metadata, first and last cell
  receiving the outer delimiters;
- **sanitise** any `|` the model emitted inside a cell — strip literal pipes
  before reassembly, otherwise a hallucinated delimiter corrupts the row.

Empty source text yields an empty translated cell, never a missing one.

---

## Task 2 — Registry text branch

**Files:** modify `app/adapters/formats/registry.py`.

Markdown has no magic number, and the current `resolve` requires
`header.startswith(signature)`. Introduce a text-format set:

```python
_TEXT_FORMATS = frozenset({"md"})
```

For a registered text format, validate that the header decodes as UTF-8 and
contains no NUL bytes, instead of comparing a prefix. **Binary formats keep the
exact signature check** — no weakening for PDF or DOCX.

A `.pdf` renamed to `.md` is rejected by the UTF-8 check unless its bytes happen
to be valid text; that residual case is accepted and stated rather than papered
over.

---

## Task 3 — Upload allow-list

**Files:** modify `app/core/services/document_service.py`.

Add `"md"` to `_SUPPORTED_FORMATS`. Extension resolution there already derives
from the sanitised filename.

---

## Task 4 — Composition and UI

**Files:** modify `app/api/dependencies.py`,
`frontend/src/features/upload/UploadForm.tsx`,
`frontend/src/features/jobs/JobCard.tsx`.

- `registry.register("md", MarkdownExtractor(), MarkdownRenderer())` in
  `get_format_registry`.
- Add `.md` and `text/markdown` to the upload `accept` list.
- Teach the download path to derive `.md` from `document.format`.

Both are public-facing contract changes and are recorded as such.

---

## Task 5 — Fixtures and tests

**Files:** create `samples/sample_en.md`,
create `tests/adapters/formats/test_markdown.py`.

Fixture must contain, in one file: a heading tree, plain paragraphs, bullet and
numbered lists, a block quote, a multi-column table **including empty cells and
a separator row**, a fenced code block, inline emphasis, and placeholder /
date / currency tokens so preservation metrics have something to measure.

Tests:

| Case | Expectation |
|---|---|
| round trip | output parses; heading, list, quote and paragraph structure intact |
| table shape | identical row and column counts before and after |
| empty cells | preserved in position |
| separator row | byte-identical, never translated |
| fenced code | byte-identical, never translated |
| prefixes | `## `, `- `, `> ` survive translation |
| hallucinated pipe | a `\|` returned by the model is stripped; row shape holds |
| adversarial | a model returning empty text for every cell still yields a valid table |

Bypass coverage, in `tests/services/` and `tests/worker/`:

| Case | Expectation |
|---|---|
| bypassed blocks | never appear in any chunk's block links |
| bypassed blocks | never produce a `block_translations` row |
| merged-cell document | job finishes `done`, **not** `completed_with_errors` |
| bypassed blocks | cost per document is unchanged versus no table |

---

## Task 6 — Documentation

**Files:** modify `ARCHITECTURE.md`, modify `DECISIONS.md`.

- Replace the "10 minutes / one module plus one registry entry" claim with the
  six-touchpoint table from the audit above.
- Record Markdown as the third supported format and why: the strongest available
  demonstration of Opaque Metadata, because table structure lives in metadata
  and is therefore immune to model output.
- State the format-dependent difference explicitly: **Markdown tables are
  translated, DOCX tables are not.** `DECISIONS.md` currently records table
  cells as a DOCX cut; without this note the documentation would contradict the
  code.

---

## Resolved: pipeline bypass for empty cells (Option A)

The blocking risk is closed. Empty table cells are real blocks that **never
enter the translation pipeline**.

Classification is by block kind, not by inspecting text — a non-empty cell
suffixed with markup is still translatable, so emptiness cannot be used as the
test:

| `kind` | Block created | Reaches chunker / LLM / cache |
|---|---|---|
| `heading`, `paragraph`, `list_item`, `quote`, `table_cell` | yes | yes |
| `table_cell` with empty `source_text` | yes | **no — bypassed** |
| `table_separator` | no | no |
| `fence_open`, `fence_close`, `fence_content` | no | no |

**1. Extraction** creates a block for every table cell, empty or not, so the row
geometry is complete on the canvas.

**2. Filter before chunking.** Add
`skip_block_ids: set[str] | None = None` to `JobService.create_jobs` /
`_group_blocks`. The Markdown adapter exposes `self.skip_block_ids` — the block
IDs that are structural only. The service filters them out of chunking in one
predictable place, inside the existing core service, not in a router or
adapter.

**3. Renderer.** `Assembly._load_translations` yields no entry for a bypassed
block, and the renderer reconstructs it from `format_metadata` alone: it emits an
empty cell with its stored `prefix` / `suffix`. Bypassed blocks therefore need
no special case at render time beyond "no translation means empty cell", which
is already how the renderer treats a block with no translation.

**4. Cache.** Bypassed blocks never reach `block_translations`, so
`INSERT OR IGNORE` never stores an empty translation and the unique key is
never poisoned.

**5. `completed_with_errors` must not trigger.** Assembly currently derives the
status from cache coverage. Bypassed blocks are structurally complete, so the
renderer must report them as *satisfied*. `RenderResult` therefore gains:

```python
passthrough_block_ids: list[str] = []
```

and assembly excludes them from the incomplete set. **A Markdown document with
merged cells must finish as `done`, not `completed_with_errors`.**

**6. Cost.** Bypassed blocks consume no provider tokens, so cost per document
does not rise with empty cells.

**Interface impact.** `DocumentExtractor` already returns `DocumentIR`, so the
adapter can expose `skip_block_ids` as a plain attribute without touching the
port protocol. The concrete `MarkdownExtractor` is constructed in
`dependencies.py`, so wiring it needs no new port.

**Cost of the change.** `_group_blocks` is tested directly; update those tests
for the new optional parameter, and add one proving a filtered document still
enqueues and still reports `done`.

---

## Out of scope

- Reference links, images, HTML blocks inside Markdown.
- Preserving inline emphasis through translation (same cut as DOCX runs).
- Frontmatter parsing.
- Refactoring the registry into a pluggable descriptor system.

---

## Critical Agent Reminders

1. **Never emit table structure from model output.** Delimiters and padding come
   from `format_metadata` only.
2. **Create a block for every empty cell, then bypass it.** The block carries the
   delimiters the row needs; the ID goes into `skip_block_ids` so it never
   reaches the chunker, the LLM, or the cache.
3. **Never let a bypassed block count as incomplete.** Report it through
   `RenderResult.passthrough_block_ids`, or the job reports
   `completed_with_errors` for a document that is actually complete.
4. **Never translate the separator row or fenced code.**
5. **Strip `|` from returned cell text** before reassembly.
6. **Keep the binary signature checks intact** when adding the text branch.
7. **Correct the "10 minutes" claim** in `ARCHITECTURE.md` — six touchpoints,
   verified.
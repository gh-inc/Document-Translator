# PDF glyph resilience — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Origin:** defect found while operating the system, not a planned stage. A
60-page landscape PDF generated from Markdown failed at render time with
`render_failed` and no diagnosable cause.

**Root cause (confirmed on `samples/platon-gliph.pdf`).** The renderer raises
`render_failed` when any character of a translated block is missing from
PyMuPDF's bundled `cjk` font. One block out of 70 carries four unsupported
characters: `U+1F3DB` (🏛), `U+FE0F` (VARIATION SELECTOR-16), and `U+0001` /
`U+0002`. The latter two are **not present in the source Markdown** — the PDF
generator placed the emoji into a font subset, and PyMuPDF emitted raw control
codes for glyphs it could not map.

**Invariants this plan must not break.**

1. **No glyph mapping tables.** Filtering by Unicode category only. A lookup of
   `✔→✓`, `₽→RUB` and friends is explicitly rejected as unmaintainable.
2. **Document and translation text never reaches logs.** Only geometry, counts,
   stage names, and character codes.
3. **Ordering is preserved:** build `render_items` → redact → insert. Breaking
   it would remove text without replacing it.
4. **No database schema change.**

**Approved decision:** hybrid A + C — warn early at extraction, degrade
gracefully at render. Strict upload rejection was rejected: one checklist icon
must not discard a 60-page document.

---

## Current state

- `app/adapters/formats/pdf.py` raises `DocumentError(RENDER_FAILED)` at the
  glyph-coverage check inside the `render_items` loop.
- Three catch-alls discard the real exception: `pdf.py` `render()`,
  `assembly.py` render step, `claim_loop.py`. The only surviving log line is
  `worker_render_failed` carrying `job_id` alone.
- Triage warnings are stored in `document_analyses.warnings` but are **never**
  surfaced by any API response (zero occurrences of `warnings` under `app/`).
- `DocumentRenderer.render` currently returns `Path`, so a renderer cannot tell
  the caller which blocks it degraded.
- `Assembly.render` computes `DONE` / `COMPLETED_WITH_ERRORS` **before**
  rendering, from cache coverage alone.

---

## Phase 1 — Diagnostics

Behaviour-neutral. Restores the ability to see why rendering failed.

**Logging policy.** Do **not** use `logger.exception` with a full traceback: a
PyMuPDF exception message can embed a fragment of the document, violating the
rule in `docs/api.md`. Log `error_type` (the exception class name), a `stage`,
and structural fields only.

**Files and edits:**

| File | Change |
|---|---|
| `app/adapters/formats/pdf.py` — `render()` | in `except Exception`, log `stage="render"`, `error_type` |
| `app/adapters/formats/pdf.py` — `_metadata_rectangle` | log `stage="metadata"`, `reason` (`page_out_of_range` / `bbox_shape` / `not_finite` / `out_of_bounds`), plus `page`, `bbox`, `page_rect` |
| `app/adapters/formats/pdf.py` — `_append_paginated_text` | log `stage="fallback"`, `reason` (`page_too_small` / `lines_per_page`) |
| `app/adapters/formats/pdf.py` — `_wrap_text` | log `stage="wrap"`, `reason="char_too_wide"` |
| `app/adapters/formats/pdf.py` — around `document.save` | log `stage="save"`, `error_type` |
| `app/adapters/formats/docx.py` — `render()` | log `stage="docx_render"`, `error_type` |
| `app/worker/assembly.py` — render step and `save_output` step | log `stage`, `job_id`, `document_id`, `error_type` |
| `app/worker/claim_loop.py` — existing `worker_render_failed` | add `error_code`, `document_id` |

**Test.** Force a render failure; assert the log records carry `stage` and
`error_type`, and that no substring of the source or translated text appears in
the captured log output.

---

## Phase C — Two-stage degradation

### Step 1 — Result model

`app/core/models.py`. This is a domain value object, not a persistence record,
so the one-to-one record/column rule is unaffected.

```python
class RenderResult(BaseModel):
    """Outcome of rendering one translated document."""

    model_config = ConfigDict(extra="forbid")

    output_path: Path
    degraded_block_ids: list[str] = Field(default_factory=list)
    fallback_blocks: int = 0
    fallback_pages: int = 0
```

### Step 2 — Port change (requires approval)

`app/core/ports.py`:

```python
async def render(
    self,
    original_path: Path,
    blocks: list[Block],
    translations: dict[str, str],
    output_path: Path,
) -> RenderResult: ...
```

Update `PdfRenderer.render` and `DocxRenderer.render` accordingly, plus every
call site and test that asserts a returned `Path`.

### Step 3 — Font singleton

```python
_RENDER_FONT = pymupdf.Font("cjk")
```

The font carries ~50k glyphs; it is currently reconstructed on every render.

### Step 4 — Two-stage filter

```python
_KEEP_CONTROL = frozenset({"\n", "\t"})


def _split_unsupported(text: str) -> tuple[str, set[str]]:
    """Return renderable text and the visible characters the font cannot draw.

    Non-printing format and control characters are dropped first, because they
    carry no meaning and are frequently artefacts of PDF font subsetting.
    Newlines and tabs survive so paragraph structure is preserved. Any visible
    character the font still cannot draw is reported to the caller.
    """
    droppable = {
        char
        for char in text
        if unicodedata.category(char) in {"Cc", "Cf"} and char not in _KEEP_CONTROL
    }
    cleaned = "".join(char for char in text if char not in droppable)
    unsupported = {
        char for char in cleaned if not char.isspace() and not _RENDER_FONT.has_glyph(ord(char))
    }
    return cleaned, unsupported
```

Newline and tab must be preserved: `\n` is category `Cc`, and dropping it would
collapse paragraph layout.

### Step 5 — Degrade instead of raise

Inside the `render_items` loop in `_render_sync`:

```python
cleaned, unsupported = _split_unsupported(translated_text)
if unsupported:
    _logger.warning(
        "pdf_block_degraded",
        block_id=block.id,
        unsupported_codes=[f"U+{ord(char):04X}" for char in sorted(unsupported)],
    )
    degraded_block_ids.append(block.id)
    continue  # stays out of render_items, so it is never redacted
render_items.append((page_number, rectangle, cleaned))
```

Because the block never enters `render_items`, it is excluded from the redaction
pass, so the original text stays intact on the canvas. **This depends on the
existing ordering and must not be reordered.**

`_render_sync` returns `degraded_block_ids` as a third element; `render()`
assembles the `RenderResult`.

### Step 6 — DOCX participates in the contract

`DocxRenderer.render` returns `RenderResult(output_path=...)` with an empty
`degraded_block_ids`: DOCX draws with the document's own fonts, so there is no
coverage constraint.

### Step 7 — Status decided after rendering

`app/worker/assembly.py` currently derives the status from cache coverage before
rendering. Change it to consider degradation:

```python
status = (
    JobStatus.DONE
    if cache_complete and not result.degraded_block_ids
    else JobStatus.COMPLETED_WITH_ERRORS
)
```

Log the degraded block count and ids (UUIDs are safe to log).

---

## Phase A — Early warning

No database change.

1. `PdfExtractor._extract_sync` collects unsupported characters across every
   block's `source_text` and returns them.
2. `DocumentIR` gains `warnings: list[str] = Field(default_factory=list)`.
3. `DocumentService.upload` returns a named `UploadResult(document,
   block_count, warnings)` instead of a growing tuple; update call sites.
4. `DocumentUploadResponse` gains `warnings: list[str] = Field(default_factory=list)`.
5. The DOCX extractor returns an empty list — no coverage constraint applies.

The warning surfaces at upload only. Persisting it would require a column and a
matching `DocumentRecord` field in lockstep, because
`tests/adapters/persistence/test_schema.py` asserts exact equality between
`model_fields` and table columns. That cost is not justified for a non-fatal
message.

---

## Tests

| Fixture | Expected |
|---|---|
| `samples/platon-gliph.pdf` | renders; exactly one degraded block; job `completed_with_errors` |
| `samples/platon-complex.pdf` | renders; zero degraded blocks |
| `samples/platon-gliph.docx` | renders; empty `degraded_block_ids` |
| `samples/platon-complex.docx` | renders; empty `degraded_block_ids` |

Unit coverage:

- `_split_unsupported` drops `U+0001`, `U+0002`, `U+FE0F`; preserves `\n`;
  reports `U+1F3DB` as still visible.
- A degraded block is not redacted — assert the output still contains its
  original text.
- Logs contain `stage` and `error_type`, and never contain document text.

`samples/platon-*.{md,pdf,docx}` become permanent regression fixtures.

---

## Out of scope

- Glyph mapping or transliteration tables.
- Changing `render_failed` into a retryable outcome — retrying cannot help when
  the font lacks the glyph.
- Hard rejection at upload (option B).
- DOCX table translation, tracked separately in
  `docs/plans/2026-10-03-docx-tables-design.md`.

---

## Critical Agent Reminders

1. **No mapping tables.** Filter by Unicode category; never translate glyphs into
   ASCII substitutes.
2. **Preserve `\n` and `\t`.** They are category `Cc`; dropping them destroys
   paragraph layout.
3. **Never reorder render_items → redaction → insertion.** A degraded block must
   leave the canvas untouched, not be erased.
4. **Never log document or translation text.** Field names, geometry, counts,
   and `U+XXXX` codes only. No full tracebacks.
5. **Decide the status after rendering**, not before.
6. **The font is a module-level singleton**, not per-render.
## Execution notes

Implemented decisions, plan corrections, verification and review are recorded in
[the execution record](2026-10-03-pdf-glyph-resilience-execution.md).
U+FE0F is category Mn; the implemented filter additionally removes Unicode
variation selectors. Overlapping redaction regions exclude degraded source
rectangles so neighboring blocks cannot erase retained text.

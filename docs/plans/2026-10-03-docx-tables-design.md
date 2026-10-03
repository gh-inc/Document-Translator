# DOCX table translation — technical design

> **Status:** implementation authorized by the user on 2026-10-03.
> Execution and verification: [DOCX tables execution](2026-10-03-docx-tables-execution.md).
>
> Companion to `2026-10-03-pdf-glyph-resilience.md`; independent of it.

**Origin:** defect found while operating the system. A DOCX translated
successfully but table content was left untranslated.

---

## Observed defect

Measured on `samples/platon-gliph.docx` and `samples/platon-complex.docx`:

| Measure | Value |
|---|---:|
| Body paragraphs extracted | 16 |
| Tables | 1 |
| Paragraphs inside table cells | **28** |
| Share of text never translated | **28 / 44 ≈ 64%** |

**Cause.** The extractor iterates `document.paragraphs`, which in `python-docx`
yields only top-level body paragraphs. Table cell paragraphs are unreachable
through that API.

**Not a regression.** This is a recorded Stage 3 scope cut
(`DECISIONS.md`, Stage 3 format measurements): "Table cells, headers, and
footers stay unchanged; their translation is outside the approved Stage 3
scope." It is nevertheless a significant product defect.

---

## Strategy — traverse the real document body

`document.paragraphs` is the wrong traversal. Read the body element and
interleave paragraphs and tables in true reading order:

```python
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


def iter_body_items(document) -> Iterator[tuple[str, int, object]]:
    """Yield (kind, index, element) for body-level content in reading order."""
    for index, child in enumerate(document.element.body.iterchildren()):
        if child.tag == qn("w:p"):
            yield "paragraph", index, Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield "table", index, Table(child, document)
```

Then expand each item: a `paragraph` yields its own block; a `table` expands
to its cell paragraphs.

**`seq` must stay monotonic across the whole document.** Context handed to the
LLM follows `seq`, so if tables were emitted separately the reading order the
model sees would diverge from the visual order and translation quality would
degrade. This is the single most important correctness requirement here.

---

## Locators in `format_metadata`

Format-owned and opaque to the core. The core still sees only `seq` and
`source_text`.

```python
# body paragraph
{"container": "body", "body_index": 3, "style": "Normal"}

# paragraph inside a table cell
{
    "container": "table",
    "table_index": 1,
    "row_index": 2,
    "cell_index": 0,
    "paragraph_index": 0,
    "style": "Normal",
}
```

`body_index` is the position of the `w:p` or `w:tbl` element inside
`document.element.body`, so ordering can be reconstructed without re-walking.

---

## Renderer

Dispatch on `container`:

- `"body"` — current behaviour: clear all runs, add one replacement run,
  preserve `paragraph.style`.
- `"table"` — resolve
  `document.tables[table_index].rows[row_index].cells[cell_index].paragraphs[paragraph_index]`,
  then apply the same run replacement.

A missing `container` key denotes a legacy body locator: resolve its
`paragraph_index` against `document.paragraphs`, not against XML body child
positions. New `"body"` locators use `body_index`.

---

## Risks to close during implementation

1. **Merged cells.** `row.cells` returns the same underlying cell multiple
   times for a horizontal merge. Deduplicate by `id(cell._tc)` before emitting
   blocks, retaining XML references across the whole table to prevent ID reuse
   and cover vertical merges too. Otherwise blocks would be duplicated.
2. **Empty cells.** Skip, consistent with the existing empty-paragraph rule.
3. **Nested tables.** A cell may contain another table. Decision for v1: skip
   explicitly and record the cut, rather than recursing. Recursion expands the
   locator model and the risk surface considerably.
4. **Backward compatibility.** Documents extracted before this change have
   `format_metadata` **without** `container`. The renderer must resolve the historical `paragraph_index` against
   `document.paragraphs` when the key is absent. Body child indices differ
   after a table; treating a legacy index as `body_index` would mis-target text.
5. **Index stability.** The renderer edits the original document rather than
   rebuilding it, so indices remain valid across the round trip.
6. **Empty paragraph inside a cell.** Do not emit a block.

---

## Out of scope for v1

- Headers and footers (`section.header` / `section.footer`) — also untranslated
  today, but a separate pass with its own locator model.
- Nested tables (explicitly skipped).
- Preserving inline formatting inside cells — the same cut already accepted for
  body paragraphs.

---

## Sizing and sequencing

Estimate **1–2 days**. Touches `app/adapters/formats/docx.py` substantially,
its existing tests, and new fixtures with populated tables.

Independent of the PDF glyph work, but apply it **after** so two behavioural
changes do not land in one regression run. Verify `samples/platon-complex.docx`
translates all 44 text units, not 16.

---

## Acceptance criteria

1. Every body and cell paragraph of `samples/platon-complex.docx` receives a
   translation (44 of 44).
2. Rendering preserves table structure and paragraph styles.
3. A document extracted before this change still renders (missing `container`
   resolves the legacy body `paragraph_index`).
4. Merged-cell tables produce one block per nonempty paragraph in each unique
   cell, without duplicate blocks from merged aliases.
5. `make test`, `make lint`, `make typecheck` pass.
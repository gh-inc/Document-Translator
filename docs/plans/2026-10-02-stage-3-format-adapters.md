# Stage 3 — Format Adapters (PDF + DOCX) Implementation Plan

> **Execution tooling:** the referenced `superpowers:executing-plans` skill is
> unavailable in this Codex session. The approved steps are executed through
> native delegation and repository tools, with disjoint ownership and review.

**Goal:** Implement two format adapter pairs — extractor + renderer — for PDF and DOCX behind the Opaque Metadata pattern, plus a format registry that resolves files by extension and magic bytes.

**Architecture:**
- **Opaque Metadata.** The core sees only `Block.seq` and `Block.source_text`. All format-specific data (`page`, `bbox`, `paragraph_index`, etc.) lives in `Block.format_metadata` and is owned exclusively by the corresponding adapter.
- **Renderer re-opens the original file as the canvas.** It places translations into the original document, preserving everything the extractor did not touch (images, headers, styles, fonts) by construction.
- **Async discipline.** All interactions with `fitz` (PyMuPDF) and `python-docx` are blocking and must run inside `asyncio.to_thread`. This includes open, iteration, text insertion, and save.
- **Security.** `FormatRegistry.resolve` reads only the first 2048 bytes to check magic bytes; it never loads the entire file into memory.

**DOCX granularity:** paragraph-level blocks. The renderer clears all existing runs in a translated paragraph, inserts a single new run with the translated text, and preserves only the paragraph-level style (`paragraph.style`). Run-level formatting inside a paragraph is intentionally **not** preserved — the LLM does not receive markup, so reconstructing runs would be unreliable.

**PDF strategy:** bbox insertion with auto-shrinking font size and a minimum readable-size floor. If the translated text still does not fit, render it on a new fallback page and record the fallback count.

**Tech Stack:** Python 3.12, PyMuPDF, python-docx.

**Current State:**
- `app/adapters/formats/registry.py` is an unwired skeleton.
- `app/adapters/formats/pdf.py` and `app/adapters/formats/docx.py` do not exist.
- No sample documents or format-specific tests.

---

## Task 1: PDF Extractor

**Files:**
- Create: `app/adapters/formats/pdf.py`

Behavior (all inside `asyncio.to_thread`):
- Open the PDF with PyMuPDF (`fitz.open`).
- Iterate pages and extract text blocks in reading order.
- For each non-empty block create a `Block`:
  - `id` = deterministic UUID derived from `document_id` + `seq`.
  - `seq` = reading-order index.
  - `source_text` = stripped block text.
  - `source_hash` = sha256 of text.
  - `format_metadata` = `{"page": int, "bbox": [x0, y0, x1, y1]}`.
- Detect scanned PDFs: if total extracted text length is below a threshold or no text blocks are found, raise a domain exception with `error_code="scanned_pdf"`.
- Return `DocumentIR` with `page_count`, `format="pdf"`, `size_bytes`.

**Step 1: Write the failing test**

```python
async def test_pdf_extractor_yields_blocks_in_reading_order() -> None:
    extractor = PdfExtractor()
    doc = await extractor.extract(SAMPLE_PDF, "document-1")
    assert doc.format == "pdf"
    assert len(doc.blocks) >= 2
    assert doc.blocks[0].seq == 0
    assert "page" in doc.blocks[0].format_metadata
```

Run: `pytest tests/adapters/formats/test_pdf.py::test_pdf_extractor_yields_blocks_in_reading_order -v`
Expected: FAIL.

**Step 2: Implement the extractor**

**Step 3: Run tests**

Expected: PASS.

---

## Task 2: PDF Renderer

**Files:**
- Modify: `app/adapters/formats/pdf.py`

Behavior (all inside `asyncio.to_thread`):
- Open the original PDF as canvas.
- Build `block_id → translated_text` from `translations`.
- For each translated block:
  - Locate the page and bbox from `format_metadata`.
  - Compute a font size that fits the bbox; shrink down to a minimum readable size (e.g., 6 pt).
  - If it fits: render into the bbox.
  - If it does not fit: create a new page at the end of the document, render the translated text there, and increment a fallback counter.
- Save to `output_path`.
- Return `output_path`.

**Exit criteria:** Render a sample PDF through `FakeProvider` and assert the output parses, contains the translated text, and records the fallback count.

---

## Task 3: DOCX Extractor

**Files:**
- Create: `app/adapters/formats/docx.py`

Behavior (all inside `asyncio.to_thread`):
- Open DOCX with `python-docx`.
- Iterate paragraphs in reading order.
- For each paragraph with non-empty text create a `Block`:
  - `format_metadata` = `{"paragraph_index": int, "style": paragraph.style.name}`.
- Return `DocumentIR`.

**Exit criteria:** Extractor test proves a sample DOCX yields expected paragraph blocks in order.

---

## Task 4: DOCX Renderer

**Files:**
- Modify: `app/adapters/formats/docx.py`

Behavior (all inside `asyncio.to_thread`):
- Open original DOCX as canvas.
- Build `paragraph_index → translated_text` map from `translations`.
- For each translated paragraph:
  - Clear **all** existing runs in the paragraph.
  - Add a single new run with the translated text.
  - Preserve only the paragraph-level style (`paragraph.style`).
  - Do **not** attempt to preserve bold/italic/font or other run-level formatting.
- Save to `output_path`.
- Return `output_path`.

**Exit criteria:** Render a sample DOCX through `FakeProvider` and assert the output parses, contains translated text, and paragraph styles survive.

---

## Task 5: Format Registry

**Files:**
- Modify: `app/adapters/formats/registry.py`

Behavior:
- Maintain an internal `dict[str, tuple[DocumentExtractor, DocumentRenderer]]`.
- `register(format_name, extractor, renderer)`.
- `resolve(file_path)`:
  - Check extension (`.pdf`, `.docx`) for fast path.
  - Validate magic bytes by reading **only the first 2048 bytes** of the file:
    - PDF: starts with `%PDF-`.
    - DOCX: starts with `PK\x03\x04` (ZIP).
  - Return `(extractor, renderer)` or `None`.

**Security reminder:** Never read the full file just to check its type.

**Exit criteria:** Registry tests prove correct resolution for PDF/DOCX and `None` for unsupported files.

---

## Task 6: Sample Document Generator

**Files:**
- Create: `scripts/generate_sample_docs.py`

Generate under `samples/`:
- `sample_en.pdf` — 2-page PDF with headings, paragraphs, and a simple table.
- `sample_en.docx` — DOCX with styled paragraphs and a table.

These files are used by format adapter tests and by the quality measurement script in Stage 9.

**Exit criteria:** Running the script produces both sample files with known text content.

---

## Task 7: Opaque Metadata Invariant Test

**Files:**
- Create: `tests/adapters/formats/test_opaque_metadata.py`

Test:
- Create `Block` instances with deliberately malformed `format_metadata` (e.g., wrong types, unknown keys).
- Pass them through `FakeProvider.translate_chunk`.
- Assert that translation succeeds and `format_metadata` survives the roundtrip unchanged.

**Exit criteria:** Test passes.

---

## Task 8: PDF Overflow / Fallback Measurement

**Files:**
- Create: `tests/adapters/formats/test_pdf_renderer.py`

Test:
- Use `samples/sample_en.pdf`.
- Translate with `FakeProvider` to a language that expands text length (e.g., German).
- Render with the PDF renderer.
- Assert the output file parses and contains the translated text.
- Report the number of fallback pages.

This produces the measurement required by `ARCHITECTURE.md` §6.6 / §17. If the fallback rate is unexpectedly high, document the decision to keep or change the default renderer in `DECISIONS.md`.

**Exit criteria:** Measurement recorded; output file validated.

---

## Task 9: Verification

```bash
make test
make lint
make typecheck
```

Expected: all green.

---

## Task 10: Commit and Backfill `TASKS.md`

Proposed task IDs:
- `DT-22`: PDF extractor + renderer
- `DT-23`: DOCX extractor + renderer
- `DT-24`: Format registry + sample generator
- `DT-25`: Opaque metadata invariant + renderer validation tests

Single-commit option:

```bash
git add app/adapters/formats scripts tests/adapters/formats TASKS.md
git commit -m "DT-22: feat(formats): implement PDF and DOCX adapters with registry"
```

---

## Critical Agent Reminders

1. **All PyMuPDF / python-docx calls inside `asyncio.to_thread`.** File open, iteration, text insertion, redaction, and save are blocking and must not run on the event loop.
2. **DOCX renderer clears runs.** Do not preserve inline formatting. Use one new run per translated paragraph and keep only `paragraph.style`.
3. **PDF renderer fallback.** Auto-shrink font to fit the bbox; if it still does not fit at the minimum size, render on a new page.
4. **Magic bytes read only first 2048 bytes.** Do not load the whole file to determine its format.
5. **Opaque Metadata.** Never validate or transform `Block.format_metadata` outside the adapter that owns it.

---

## Execution record — 2026-10-02

The user authorized orchestration, delegation, implementation, verification, and
commits. DT-22–DT-25 were added to TASKS.md before implementation. Existing core
models, format ports, and public API contracts were retained.

### Ownership and dependencies

- PDF worker: `pdf.py` and PDF unit tests (DT-22).
- DOCX worker: `docx.py` and DOCX unit tests (DT-23).
- Registry/sample worker: `registry.py`, generator, samples, and registry tests
  (DT-24).
- Orchestrator: shared safe document errors, opaque-metadata and sample
  integration tests, PDF measurements, documentation, acceptance checks,
  commits, and task hash backfills (DT-25).

The orchestrator supplied `DocumentError` and catalog codes before adapters
were implemented. Workers read installed PyMuPDF/python-docx source, used only
existing dependencies, and did not commit. Completed workers independently
reviewed the other adapters and registry.

### Review corrections and operating limits

- Stored uploads have no extension. Signature routing supports them, while
  unsupported suffixes and suffix/signature mismatches are rejected. The
  2048-byte ZIP signature does not prove valid OOXML; extractor failures map
  safely to `corrupt_file`.
- PDF redaction removes source text for supplied translations and preserves
  image/vector artwork. Missing translations retain source content. Shrinking
  attempts the minimum 6 pt size exactly; overflow paginates without truncation.
- Bounding boxes extracted slightly beyond a page edge are clipped by the PDF
  adapter, avoiding rejection of its own metadata. Invalid metadata still
  fails with safe `render_failed` errors.
- A short valid text PDF must not be labelled scanned. Only a PDF with no
  non-whitespace text is rejected as `scanned_pdf`; this follows the
  “Pipeline” requirement to detect absent text layers rather than invent OCR.
- PDF uses a bundled Unicode font and checks glyph coverage, avoiding a silent
  Helvetica fallback when system fonts are absent. Unsupported glyphs fail
  safely; complex shaping/RTL remains outside the stated PDF fidelity scope.
- DOCX extraction covers top-level paragraphs. Rendered paragraphs keep their
  paragraph styles/properties but lose inline formatting and hyperlinks.
  Table cells, headers, and footers remain unchanged on the original canvas.
- Fallback measurement uses structured per-render logs, preserving the
  existing `Path` return type. FakeProvider's `[de]` prefix is not a real
  German translation; a separate synthetic expansion exercises overflow.
- Actual-thread pytest runs require execution outside this tool sandbox;
  production async code and tests were not altered to bypass that limitation.

Final measurement and acceptance results are recorded in DECISIONS.md and
TASKS.md after verification.

**Final results:** `make test` — 238 passed, 1 live test deselected;
`make lint` — clean; `make typecheck` — clean (22 source files). All 30 format
tests pass. Both sample runs (FakeProvider prefix and synthetic >=30% expansion)
require 3/12 fallback blocks and three appended pages, with every translation
preserved. Font fitting and insertion share the exact bundled font buffer;
insertion stays inside the original bbox after rejecting a 2 pt expansion that
could overlap neighboring content. The nine ordinary text blocks fit, so bbox
insertion remains the default with table limitations explicitly documented.
The two existing Pydantic `register` warnings remain. Real OpenAI calls were not
run. The user-authorized single implementation commit and subsequent hash
backfill deliver DT-22–DT-25; unrelated initial workspace files are excluded.

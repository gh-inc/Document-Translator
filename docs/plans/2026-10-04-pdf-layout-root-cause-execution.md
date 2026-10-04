# PDF layout root-cause execution — DT-104–DT-107

Investigation requested on 2026-10-04 against
[the source plan](2026-10-03-pdf-layout-root-cause.md).
Three delegated owners investigated cache/persistence, PDF overflow/glyphs,
and stale extraction/isolated worker execution. Root reviewed evidence and
performed final acceptance. No real provider calls or writes to `/data/app.db`.

## Findings

The 45-page PDF is reproducible with the existing English translations:
70/70 blocks have cache coverage, 42 blocks cannot fit their original rectangles
and each creates an appended page (3 + 42 = 45). One unsupported glyph block
retains its original text. This is a rendering-policy limitation, not evidence
that cache text is attached to a foreign rectangle.

A separate source-retention defect exists with incomplete translations:
missing blocks are skipped before their rectangles are protected from neighboring
redactions. Overlapping translated blocks can therefore erase untranslated
source text. Historical six-page output is not a reliable fidelity baseline.

| Hypothesis | Verdict | Evidence |
| --- | --- | --- |
| H1: cache attaches translations to foreign layout | Not reproduced for investigated PDF | Exact source hashes map to current block IDs; full-cache render reproduces overflow |
| H2: stored extraction is stale | Confirmed for two DOCX files, excluded for investigated PDF | Two historical DOCX files store 16 body blocks versus 44 fresh blocks (28 table); PDF has 70/70 exact matches |
| H3: unsupported glyph retains source | Confirmed | seq=4 contains controls and U+1F3DB; renderer degrades that block |
| H4: per-block overflow appends pages | Confirmed | 42 fallback blocks, 42 appended pages, 45 total pages |
| H5: metadata damaged in persistence | Not reproduced | Fresh/stored PDF metadata agree; isolated PDF/DOCX round-trip probes |
| Bulk model changed between PDF jobs | Excluded for these five jobs | All use gpt-4o-mini; no counterfactual model-quality claim |
| Untranslated source erased by overlap | Confirmed additional defect | Missing seq=4 loses “Ключевые идеи” under seq=5 redaction |

## Read-only audit and clean reproduction

All ten uploaded files match their content-digest document IDs. Counts and
comparisons below come from fresh extraction of the persisted original files;
comparisons align stored/fresh blocks by sequence.

| File | Stored / fresh blocks | Stored / fresh table blocks | Matching source hashes / metadata |
| --- | --- | --- | --- |
| diag-01.docx | 44 / 44 | 28 / 28 | 44 / 44 |
| platon-gliph.md | 51 / 51 | 0 / 0 | 51 / 51 |
| sample_en.pdf | 12 / 12 | 0 / 0 | 12 / 12 |
| platon-gliph.pdf | 70 / 70 | 0 / 0 | 70 / 70 |
| platon-gliph.docx | 16 / 44 | 0 / 28 | 8 / 0 of 16 stored |
| platon-complex.docx | 16 / 44 | 0 / 28 | 8 / 0 of 16 stored |
| platon-complex.pdf | 36 / 36 | 0 / 0 | 36 / 36 |
| platon.pdf | 9 / 9 | 0 / 0 | 9 / 9 |
| Distributed Systems Snapshot.pdf | 1277 / 1277 | 0 / 0 | 1277 / 1277 |
| Distributed Systems Snapshot Landscape.pdf | 1268 / 1268 | 0 / 0 | 1268 / 1268 |

Phase 3 used fresh extraction into a private temporary SQLite database, seeded
from 66 unique existing English cache rows corresponding to 70 blocks. Log
capture started before the worker. Result: 70 hits, 0 misses, 0 provider calls,
42 fallback blocks/pages, one degraded block, and 45 output pages. Every
nondegraded translated string was recovered from the output. The terminal
status is `completed_with_errors`, correctly reflecting the retained glyph block.

Local evidence paths (temporary, not deployment artifacts):
`/tmp/dt104_live_all_doc_audit.py`, `/tmp/dt104_export_live_en_cache.py`,
`/tmp/dt104-live-en-cache.json`, `/tmp/dt104_phase3_cached_pdf.py`,
`/tmp/dt104-phase3-eo980yh4/worker.log`, and
`/tmp/pdf_layout_overflow_probe.py`. SQLite diagnostic readers explicitly used
`mode=ro`; the all-document reader also set `query_only=ON`. These one-off
SQL scripts stay outside application code.

The committed translation fixture records its source document digest and job/key
provenance. The offline probes use it to reproduce the same renderer and worker
outcomes. Root visually inspected the original, repeated full-cache output,
and missing-block output: emptied original regions and appended text agree
with the measured mechanism.

Historical `af889892` has 6 pages: pages 1–2 match source text, source page 3
shrinks from 729 to 453 extracted characters (Cyrillic 488 to 299), and its
three appended pages contain 193 Latin letters. Its historical cache state
cannot be reconstructed from the current post-migration cache, so the partial
redaction reproduction establishes a mechanism, not a complete historical
causal trace. The five persisted PDF jobs all record `gpt-4o-mini`; no provider
comparison was performed. A changed triage plan can invalidate cache reuse,
but does not transfer positioning metadata between documents.

## Scope and decisions

This delivery completes the investigation and commits its probes and evidence.
It does not change production behavior. The source plan explicitly reserves
fallback policy and fix selection to the owner. New re-extraction endpoints or
schema/version contracts also require explicit approval under AGENTS.md.
The confirmed source-retention defect needs a separate targeted correction;
clean-cache success must not be generalized to partial-cache fidelity.

Architecture references: **Layering & the Document IR**, **Data model (SQLite,
WAL)**, **Testing strategy**, and **Observability**. Metadata stays opaque in
production core code; format probes inspect it at the adapter test boundary.

## Corrections to source-plan assumptions

- `direct = dict(from_cache)` is not an independent cache oracle. Probes seed
  expected values separately and include repeated text and another-language decoys.
- PDF byte equality is not a stable layout oracle; generated file IDs can differ.
  Compare text, coordinates and rendered page pixels instead.
- `pdf_render_completed` exists and logs fallback counters. A worker-level
  completion event is absent, but the claim that no fallback logs exist is false.
- Historical `af889892` is only identical on its first two source pages. Page 3
  loses text and three appended pages contain translated Latin text.
- H2 and H1 should not be conflated: old DOCX extraction explains missed table
  paragraphs without demonstrating cache contamination or persistence corruption.

## Acceptance and usage

The three probe files add six offline tests: independent cache mapping,
PDF/DOCX metadata round-trips, measured overflow/degradation, partial-cache
source loss, and isolated worker replay. Independent delegated review found
no blockers. The source-loss test is a characterization of a known defect,
not a desired-behavior regression test; DT-108 records the correction and its
expected assertion must change when the fix lands.

Verification runs outside sandbox because even trivial `asyncio.to_thread`
stalls inside this environment. Initial full-suite runs exposed two probe
issues (duplicate-text coordinate matching and cached structlog instances);
the probes now disambiguate by bbox and explicitly rebind captured loggers.
Final acceptance: `make test` **712 passed, 2 live deselected**;
`make lint` clean (**187 files**); `make typecheck` clean (**61 source files**).
Six pre-existing Pydantic warnings remain. Log: `/tmp/dt107-test-final.log`.
No deployment, push or production data mutation was performed.


## Delivery and agent token accounting

Investigation/probe delivery commit: `13b8b7b` on
`dt-104-pdf-layout-investigation`. This documentation follow-up backfills the
task hashes and usage checkpoint; no amend, rebase or push.

Local `token_count` records across root and three delegated sessions, sampled
after the delivery commit: **input 27,638,135; output
110,371**. Input includes **27,089,520 cached
tokens** and repeated context across model turns; output is the recorder's
output total, including reasoning. This is token usage, not a provider billing
estimate. It excludes the subsequent hash-backfill/usage-commit/final-report
turns, whose usage cannot be included in a report written before they finish.
Raw checkpoint: `/tmp/dt107-token-checkpoint.json`.

| Session | Input | Output |
| --- | ---: | ---: |
| 01a1047e-c593-7d71-89da-1916b0670037 | 7,595,644 | 12,761 |
| 01a10483-0bc1-7152-a4cf-baa693e14724 | 6,769,137 | 24,373 |
| 01a10483-1c70-75a2-b057-f0e2b393870d | 4,996,538 | 32,887 |
| 01a10483-2b57-7b62-8342-cf80e96d8284 | 8,276,816 | 40,350 |

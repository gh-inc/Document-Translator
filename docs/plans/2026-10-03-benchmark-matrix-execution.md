# DT-92 benchmark matrix execution

Plan: [benchmark matrix](2026-10-03-benchmark-matrix.md).

## Decomposition and ownership

1. Metrics agent: core quality module and mathematical unit tests (Task 1).
2. Corpus agent: real published EN-DE sample, licence/provenance and reproducibility (Task 2).
3. Matrix agent: existing measurement CLI, models loop, isolated runs, accounting/report tests (Task 3 and script extraction wiring).
4. Root: integration, real two-model measurement, measured documentation (Task 4), independent review, full verification and commits.

## Plan review

The plan is approved by the execution request. No REST, MCP or DB contracts change.
Core remains stdlib-only; corpus reference is copied verbatim from published data.
Script owner removes old metric implementations and imports the metrics agent's module.
Corpus .txt source requires a DOCX input for the existing command: generate a temporary
DOCX from the source lines for measurement, without a new benchmark script.
Identical literal suffixes in both corpus files may provide preservation coverage;
disclose any such adaptation and its limitation, preserving original published prose.
Matrix USD per million mixed input/output tokens is the observed weighted unit cost,
not a new provider rate. Request counts must state whether bulk or all phases are counted.
Without --models, existing report shape and behavior remain compatible.
User's request authorizes the required small live two-model CLI comparison.

Pre-existing user edits in PROMPTS, roadmap, other execution plans, OVERVIEW and
DOCX design remain excluded from delivery. Work uses branch benchmark-matrix.

## Verification and measurement

- Metric extraction: AST-identical to prior implementations; 18 quality tests
  pin mathematics, exact multisets, duplicates, categories and empty semantics.
- CLI: 18 focused tests cover real FakeProvider pipeline isolation, per-model
  token/cost accounting, guard failures, validation, output streams, default
  compatibility and failure without a fabricated matrix.
- Corpus: independent archive reproduction matched all 20 pairs, disclosed
  suffixes and archive/member/sample hashes; publisher licence confirmed.
- Final offline make test: 624 passed, 2 live tests deselected, 88.62 seconds.
- Backend make typecheck: clean, 61 source files. Initial make lint passed
 169 files; final staged delivery snapshot make lint passed (168 files).

Two live two-model runs completed. The first preceded two reporting fixes
(scoping legacy usage exclusions and labeling reference/back-translation mode).
The final CLI was rerun without numeric/prompt changes. Both JSON reports are
retained, including the pilot's historical exclusion wording, with its scope
clarified in DECISIONS. Final mini/4o chrF: 71.3698/70.8356; preservation 14/19
for both. Pilot score ordering differs, so no winner is inferred. Four model
measurement runs total $0.05823335 in known application service usage estimates;
final matrix alone $0.02925670. This is separate from agent token accounting.

Independent review closed two report-presentation findings and the archive
member path-prefix correction. The corpus and score limitations are explicit.
No REST/MCP/schema additions, push, dependencies, or local-model provider.

Final scoped review verified saved JSONs byte-for-byte against raw live output,
all numeric figures and preserved pilot variation. The last terminology comment
was resolved: four model measurement runs, not four provider calls.

A new unrelated user draft, docs/plans/2026-10-03-triage-agent-efficiency.md,
appeared mid-task and fails the global Markdown formatter check. It is preserved;
the staged delivery snapshot was verified in /tmp without including or editing it.
OVERVIEW remains untouched because this matrix is documented in DECISIONS,
README and the execution record, with build/service accounting kept separate.

## Token accounting

Record task-specific root usage delta and all DT-92 delegated sessions at delivery.

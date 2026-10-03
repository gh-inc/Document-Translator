# Benchmark matrix and quality baseline — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Origin:** the brief asks for one measured translation-quality number. We
currently report back-translation chrF (85.27), which measures pipeline
consistency rather than translation quality. `DECISIONS.md` still carries
"Supplied-reference chrF | not measured".

**Approved decisions (user):**

1. **Extract the metrics into the core.** `chrf_score` and `preservation_metrics`
   move to `app/core/quality.py` as pure functions with unit tests. Reporting
   unverified numbers contradicts the project's own standard.
2. **No new benchmarking script.** Extend `scripts/measure_quality.py` with a
   `--models` flag. One source of truth for metric implementations.
3. **Reference corpus: public parallel data.** Do not commission a human
   translation for the MVP.
4. **Local models are deferred.** Ollama / GGUF goes into Future Work.

---

## What already exists

Do **not** rebuild any of this — `scripts/measure_quality.py` (652 lines)
already implements:

- `chrf_score()` — corpus chrF, β=2, orders 1–6, whitespace-excluded
- `preservation_metrics()` — placeholder / date / currency / number multiset
  comparison with `preservation_percent`
- `--reference` accepting a reference as PDF, DOCX, or UTF-8 text
- `require_live_provider()` — fails loudly under `LLM_PROVIDER=fake`
- Full pipeline drive: upload → triage → job → worker → render
- JSON report plus a human-readable report

**The actual gap is three items:** no reference file, no model loop, no
comparison matrix.

---

## Task 1 — Extract metrics into `app/core/quality.py`

**Files:** create `app/core/quality.py`, create `tests/core/test_quality.py`,
modify `scripts/measure_quality.py`.

Move the following from the script, unchanged in behaviour:

- `_PLACEHOLDER_RE`, `_DATE_RE`, `_CURRENCY_RE`, `_NUMBER_RE`, `_TOKEN_RE`,
  `_CURRENCY_CODES`
- `_preservation_tokens()`
- `chrf_score()`
- `preservation_metrics()`

`app/core/quality.py` imports only the standard library. It must not import
from `adapters/`, `api/`, `mcp_server/`, or `worker/` — `tests/test_architecture_contracts.py`
enforces that direction and will fail otherwise.

The script imports from the new module and keeps a single implementation. Delete
its local copies; do not leave a shim that could drift.

**Unit tests.** Prove the mathematics, not just that it runs:

| Case | Expectation |
|---|---|
| identical text | `100.0` |
| completely disjoint text | `0.0` |
| empty candidate or reference | `0.0`, no exception |
| whitespace-only difference | `100.0` (whitespace is excluded) |
| `beta <= 0` or `max_order <= 0` | `ValueError` |
| a single changed character | score strictly between 0 and 100 |
| partial n-gram overlap | higher than disjoint, lower than identical |

Preservation tests:

| Case | Expectation |
|---|---|
| all numbers preserved | `100.0` |
| one number dropped from three | below 100, matches the multiset difference |
| repeated literal | counted per occurrence, not once |
| no tokens in source | category reports `None`, not `0` |
| reordering without loss | `100.0` (multiset, not sequence) |

The last two matter: they pin the documented semantics so a future edit cannot
quietly change what the percentage means.

---

## Task 2 — Obtain the reference corpus

**Files:** create `samples/golden_en.txt`, `samples/golden_de_ref.txt`, create
`samples/golden_dataset.md`.

### Hard constraint

**The German reference must be copied from a real published parallel corpus. It
must never be authored, paraphrased, or reconstructed from memory.** A reference
we wrote ourselves would make chrF meaningless — it would measure similarity to
our own guess at a translation, and the resulting number would be presented as
objective evidence. This is the exact failure mode `DECISIONS.md` already
condemns ("an invented number destroys trust").

Consequently:

- Never label self-written text as FLORES, WMT, or any other corpus.
- Record provenance in `samples/golden_dataset.md`: corpus name, split
  (for example `devtest`), sentence IDs, licence, retrieval date, and URL.
- Verify licence terms permit redistribution in the repository. FLORES-200 is
  **CC-BY-SA 4.0**; attribution and share-alike obligations apply and must be
  honoured in `samples/golden_dataset.md`. If a licence forbids committing the
  text, commit only the identifiers plus a fetch script and record that.

### Corpus choice

FLORES-200 EN-DE `devtest` is the industry standard for scoring LLM translators
and is the right default. If its licence or retrieval blocks us, fall back to a
freely redistributable parallel set (for example Tatoeba/OPUS EN-DE sentence
pairs) and record the substitution and its licence.

### Placeholder coverage

The current sample contains no dates, currency, or placeholders, so those
categories report `None` and prove nothing. Select or construct source sentences
that contain:

- `{{placeholder}}` and `%s` forms
- at least two dates in different formats
- at least two currency amounts with different symbols and ISO codes
- percentages and plain numbers, including repeats

The **source** sentences may be selected or lightly adapted to include these
tokens. If the source is adapted, it must be adapted identically in both files
and the adaptation disclosed in `samples/golden_dataset.md` — otherwise the
reference silently stops matching its source and chrF drops for a reason that
has nothing to do with the model.

### Known limitation to record

FLORES sentences are short prose. They do **not** exercise the pipeline's hard
parts: tables, 60-page documents, character-level reflow, PDF fallback pages.
Reference chrF therefore validates translation quality only, not document
fidelity. Say so in `DECISIONS.md` rather than presenting it as broader than it
is.

---

## Task 3 — Model loop and comparison matrix

**Files:** modify `scripts/measure_quality.py`.

Add:

```
--models gpt-4o-mini,gpt-4o
```

Behaviour:

- Defaults to the single configured model, preserving today's behaviour exactly.
- Each model runs as an isolated measurement against the same reference.
- Cost comes from `CostCalculator`; report USD per 1 000 000 tokens plus total
  run cost, so the matrix is comparable across models of different output sizes.
- Keep `require_live_provider()` at the top of every run.
- Emit both the machine-readable JSON report and a Markdown table.

Matrix columns:

```
| Model | Cost / 1M tokens in+out | Run cost USD | chrF | Preservation % | Requests |
```

Requests is worth including: it exposes how many round trips a model needed,
which is the leading indicator of latency and of agentic over-spending.

Each model must run against its own pipeline invocation so job IDs, caches and
attempts cannot bleed between runs. Report each model's tokens separately rather
than summing.

---

## Task 4 — Documentation

**Files:** modify `DECISIONS.md`.

- Add the measured matrix to the measured-numbers section, with the model list,
  corpus provenance, and run date.
- Replace "Supplied-reference chrF | not measured" with the measured figure.
- Keep the "population p95" and "before/after parallelism" entries marked not
  measured; this change does not address them.
- Add **Future work: local model provider (Ollama / GGUF)** — a local
  quantized model is the natural bridge between `FakeProvider` (free, blind to
  prompt quality) and `OpenAIProvider` (real prompt adherence, costs money), and
  would catch prompt-schema breakage without spend. Explicitly deferred as
  out of scope for a three-day assessment.
- Note in `OVERVIEW.md` §13 that build-cost accounting and service-cost
  accounting remain separate, if the matrix is added there.

---

## Exit criteria

1. `app/core/quality.py` holds the only implementation of both metrics.
2. Metric unit tests pass and pin the semantics above.
3. `samples/golden_de_ref.txt` carries verifiable corpus provenance and licence.
4. `--models` produces the Markdown matrix for at least two models.
5. `DECISIONS.md` reports reference-based chrF with its scope limitations.
6. `make test`, `make lint`, `make typecheck` pass.

---

## Out of scope

- Ollama / GGUF provider (deferred to Future Work).
- Commissioning a domain-specific human translation.
- Extending the reference corpus to tables or multi-page documents.
- Per-model prompt tuning; the matrix compares models as they ship.

---

## Critical Agent Reminders

1. **Never author the reference translation.** Copy it from a published parallel
   corpus. A self-written reference invalidates the entire measurement.
2. **Record provenance and licence** in `samples/golden_dataset.md` before
   committing corpus text.
3. **One implementation of each metric.** Delete the script's copies; do not
   leave a drifting shim.
4. **Keep the fail-loud rule.** Never report a matrix produced by
   `FakeProvider`.
5. **Default behaviour unchanged.** Without `--models`, the script must behave
   exactly as today.
6. **State what the reference does not cover.** FLORES prose is not a document
   fidelity benchmark.
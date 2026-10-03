# Triage agent cost observability — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Origin:** defect found in operation. Triage runs a tool-calling agent loop
whose context grows across turns, yet none of its token spend was recorded. The
system reported bulk-translation cost accurately and triage cost not at all —
`DECISIONS.md` already concedes "Total provider bill per document | not
measured".

**Approved decisions (user):**

1. **Port change approved.** `TriageAgent.analyze` returns a `TriageResult` DTO
   so usage travels alongside the domain object.
2. **Cost calculator approved.** Add cached-token awareness via
   `estimate_usage`; extend the pricing table.
3. **Metrics approved.** Add `llm_triage_cost_usd_total` and triage token
   counters.
4. **Attribution approved — document-level.** Triage is a document expense;
   chunk generation is a job expense. One triage run across a three-language
   batch is costed once, never divided or duplicated.
5. **Retry accounting.** Overwrite the current-plan columns on re-triage, and
   increment the running-total columns on every run.
6. **Metrics sourcing.** The `_total` columns, not an in-process counter — see
   "Decision" below.

---

## Verified SDK facts (read from the installed source)

The Agents SDK version in `.venv` (`openai-agents 0.22.3`) does **not** expose
the API commonly assumed:

| Assumed | Actual |
|---|---|
| `result.usage` | `RunResult` has **no** `usage` field |
| `usage.prompt_tokens` | `Usage.input_tokens` |
| `usage.completion_tokens` | `Usage.output_tokens` |

Correct access points:

- **`result.context_wrapper.usage`** — a `Usage` already aggregated across the
  entire run. Fields: `requests`, `input_tokens`, `output_tokens`,
  `total_tokens`, `input_tokens_details` (with `cached_tokens`),
  `output_tokens_details` (with `reasoning_tokens`), `request_usage_entries`.
- `result.raw_responses[*].usage` — per-request `ModelResponse.usage`, optional.

Treat `context_wrapper.usage` as possibly absent and default to zero.

---

## Open conflict: `/metrics` is not an event counter in this codebase

The instruction was to `inc()` a Prometheus counter on every run. That assumes
classic in-process counters. This project deliberately does not use them:
`/metrics` builds a **fresh `CollectorRegistry` per scrape** and derives values
from durable database reads, explicitly so repeated scrapes cannot double-count
and so totals survive a restart.

An in-process counter would therefore break both properties: it would reset on
restart and it would contradict the existing snapshot design.

**Decision (user approved): option B — running totals on the same row.**

The row carries two roles, and keeping them distinct is what makes the
accounting auditable:

- `cost_usd`, `tokens_in`, `tokens_out` — **current active plan**, overwritten
  on every re-triage.
- `cost_usd_total`, `tokens_in_total`, `tokens_out_total` — **cumulative**,
  incremented on every run including retries.

`/metrics` reports the `_total` columns, so absolute LLM spend accumulates
across retries while remaining a restart-safe durable snapshot. Three extra
columns, no new table. Each run's delta is logged through structlog, so a running
total can always be explained by its individual increments — which is the
condition that made an accumulated field auditable in the first place.

Rejected alternatives, recorded so they are not revisited:

- **(A) Snapshot only.** A retried triage's cost would be invisible, losing
  exactly the spend this change exists to surface.
- **(C) In-process `Counter`.** Lost on restart, duplicates the snapshot
  counters already present, and contradicts the comment in
  `app/api/routers/health.py`.

---

## Task 1 — Schema and record model

**Files:** `app/adapters/persistence/schema.sql`, `app/core/models.py`,
`app/adapters/persistence/repositories.py`.

Add to `document_analyses`:

```sql
tokens_in  INTEGER NOT NULL DEFAULT 0,
tokens_out INTEGER NOT NULL DEFAULT 0,
cost_usd   REAL    NOT NULL DEFAULT 0.0,
cost_usd_total   REAL    NOT NULL DEFAULT 0.0,
tokens_in_total  INTEGER NOT NULL DEFAULT 0,
tokens_out_total INTEGER NOT NULL DEFAULT 0,
```

The first three describe the current active plan and are **overwritten**; the
`_total` trio is **incremented** on every run, retries included.

Add all six to `DocumentAnalysisRecord` **in the same order**.
`tests/adapters/persistence/test_schema.py` asserts exact tuple equality between
`model_fields` and table columns; a mismatch fails the suite.

Update the row mapping in every read and write path.

**Test.** `test_record_fields_match_table_columns` passes for
`document_analyses`; existing rows default to zero.

---

## Task 2 — Cached-aware pricing

**Files:** `app/adapters/llm/pricing.py`, `app/core/ports.py`.

An agent loop resends a growing context every turn, so most input tokens arrive
through prompt cache and bill at roughly a tenth of the input rate. Billing all
input at full price would overstate triage cost several-fold — closing the gap
with a wrong number is worse than leaving it open.

Extend `CostCalculator`:

```python
def estimate_usage(
    self,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cached_tokens_in: int = 0,
) -> float: ...
```

Pricing becomes three rates per model (`input`, `cached_input`, `output`), and

```python
uncached = tokens_in - cached_tokens_in
return (
    uncached * prices["input"]
    + cached_tokens_in * prices["cached_input"]
    + tokens_out * prices["output"]
) / 1_000_000
```

Guard `cached_tokens_in` within `[0, tokens_in]`. Keep `estimate` unchanged for
existing callers.

**Tests.** `cached_tokens_in=0` matches `estimate` exactly; cached tokens
cheaper than uncached; negative and over-range inputs rejected; unknown model
raises.

---

## Task 3 — Agent returns usage

**Files:** `app/core/models.py`, `app/core/ports.py`,
`app/adapters/llm/triage_agent.py`, `app/adapters/llm/fake_triage_agent.py`.

```python
class TriageResult(BaseModel):
    """A triage plan plus the provider usage that produced it."""

    model_config = ConfigDict(extra="forbid")

    plan: TranslationPlan
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0
    requests: int = 0
```

`TriageAgent.analyze` returns `TriageResult`. In `OpenAITriageAgent.analyze`,
read `result.context_wrapper.usage` defensively and populate the fields.

On the failure path, attach usage to `ProviderError(tokens_in=…, tokens_out=…,
model=…)` — the exception already accepts these and currently always receives
zeros, so failed triage is invisible today.

`FakeTriageAgent` returns deterministic, distinct values so financial tests
assert something real rather than passing trivially against zeros.

**Tests.** Usage aggregates across several mocked `raw_responses`; absent usage
yields zeros; `FakeTriageAgent` usage is stable and non-zero.

---

## Task 4 — Persistence and overwrite semantics

**Files:** `app/core/ports.py`, `app/core/services/triage_service.py`,
`app/adapters/persistence/repositories.py`.

`DocumentRepository.save_analysis` takes the usage figures and computes
`cost_usd` in one place, inside the service. `TriageService.run` writes the
result.

On re-triage the two roles diverge deliberately:

- the current-plan columns are **overwritten**, because the row describes the
  plan now in force;
- the `_total` columns are **incremented** by the same values, because absolute
  spend is what must survive the retry.

`discard_degraded_analysis` deletes the row before a retry re-publishes it. That
would also drop the running totals, so this path must **preserve them** — read
the totals, delete, and carry them forward into the new row. Losing them here
would silently reset the cumulative series on exactly the retry path the change
exists to make visible.

**Tests.** A re-triage overwrites current values while increasing the totals;
`discard_degraded_analysis` preserves the totals across delete-and-republish;
totals never double-count a single run.

---

## Task 5 — Observability

**Files:** `app/core/services/health_service.py`,
`app/api/routers/health.py`, `app/core/services/triage_service.py`.

- structlog: `triage_cost_recorded` with `document_id`, `tokens_in`,
  `tokens_out`, `cached_tokens_in`, `cost_usd`, `model`, `requests`, and the
  increment applied to the running totals. **Never** log document or translation
  text. The per-run delta is what makes the cumulative series auditable.
- `/metrics`: add `llm_triage_cost_usd_total` and
  `llm_triage_tokens_total{input,output}` sourced from the `_total` columns, so
  a retried triage is visible as additional spend.
- Keep the fresh-registry-per-scrape pattern. Total document cost = bulk jobs +
  triage; expose them as separate series rather than silently summing, so a
  reader can see both.

**Tests.** Snapshot contains the new series; repeated scrapes do not accumulate;
a retried triage raises the cumulative series exactly once per run.

---

## Task 6 — Documentation

Update `DECISIONS.md` measured numbers to replace "Total provider bill per
document | not measured" with the triage-inclusive figure, keeping any residual
ambiguity (uncheckpointed ambiguous usage) stated rather than dropped.

---

## Out of scope

- A `triage_attempts` table with per-tool-call granularity.
- Per-request cost breakdown surfaced over MCP or the API; `request_usage_entries`
  stays internal.
- Retroactive reconstruction of triage cost for documents analysed before this
  change — that spend is permanently unrecorded and must be said so.

---

## Critical Agent Reminders

1. **`RunResult` has no `usage`.** Use `result.context_wrapper.usage`, and the
   fields are `input_tokens` / `output_tokens`, never `prompt_tokens` /
   `completion_tokens`.
2. **Read usage defensively.** It can be absent; fall back to zeros.
3. **Price cached input at the cached rate.** Agent loops are cache-dominated.
4. **Record fields in the same order as columns.** The parity test compares
   tuples exactly.
5. **Never log document or translation text.**
6. **Overwrite current values, increment totals.** One row, two roles: the plan
   now in force versus absolute spend ever incurred.
7. **Preserve running totals across `discard_degraded_analysis`.** That helper
   deletes the row; totals must be carried into the republished one, or the
   cumulative series resets on exactly the retry path being instrumented.
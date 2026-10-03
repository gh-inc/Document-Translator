# Total cost accounting

Everything this project cost, across three separate spending channels, with the
evidence behind each number and the gaps stated rather than smoothed over.

**As of 2026-10-03.** Two quantities are deliberately kept apart, because
conflating them is the easiest mistake to make here:

- **Cash outlay** — subscription quota and chat plans actually consumed. This is
  money that left an account.
- **Metered-equivalent value** — API list price applied to token counts. For
  Codex sessions this is a *valuation*, because those sessions ran on a
  subscription; it is not an invoice.

Provider API spend is real money under either reading, and is reported
separately from development because it is two orders of magnitude smaller.

---

## 1. Headline

| Basis | Development | Provider API | Total |
|---|---:|---:|---:|
| **Cash** (subscription/chat quota actually consumed) | ~$7.1 – $8.1 | **$0.07 – $0.08** | **~$7.2 – $8.2** |
| **Metered-equivalent** (OpenAI list price) | ~$14.5 – $16.0 | **$0.07 – $0.08** | **~$14.6 – $16.1** |

The cash row excludes Codex entirely: those sessions ran on a subscription, so
their $7.27 is a valuation with no separate invoice. The metered row adds it, plus
the uncounted post-snapshot sessions from §3.3.

Provider API spend is **under 0.5%** of what it took to build the system. The
honest reading is that the LLM is a rounding error next to the engineering, and
that the interesting cost story is *within* the provider spend — where triage
costs roughly three times the translation it configures (§6).

Three quantities are in play. Do not merge them:

1. **$7.27** — metered value of 8 Codex build sessions. A valuation.
2. **~$5 + ~$2–3** — OpenCode Go (Kimi models) and Gemini Pro chat. Cash, but
   different providers, so not part of item 1.
3. **$0.07–0.08** — OpenAI API spend on the product itself. Cash, and the only
   figure that is both metered and real.

---

## 2. Sources and their trust level

| Source | What it gives | Trust |
|---|---|---|
| `docs/measurements/*.json` | Provider-reported usage and application-estimated cost for 3 real runs | **High** — machine-readable, reproducible |
| `app/adapters/llm/pricing.py` | gpt-4o-mini / gpt-4o rates, "verified against the official OpenAI model pages on 2026-10-03" | **High** — dated and sourced |
| OpenAI model page for `gpt-6.1-sol` | $2.00 / $0.10 / $10.00 per 1M (input / cached input / output) | **High** — verified 2026-10-03 |
| `OVERVIEW.md` §13 | Codex session token counts (8 sessions) | **Medium** — session log only; see §7 |
| `docs/assessment_context/roadmap.md` | Per-stage token distribution | **Medium** — reliable as distribution, does not reconcile with the session log |
| This document's estimates | Unrecorded triage and live-test spend | **Low** — derived, with the method stated per line |

---

## 3. Development cost

Full detail and per-stage distribution live in
[`OVERVIEW.md` §13](../OVERVIEW.md). Summary, with the rates now independently
verified:

### 3.1 Codex build sessions (metered-equivalent)

8 sessions, model `gpt-6.1-sol`. Rates confirmed against
`https://developers.openai.com/api/docs/models/gpt-6.1-sol` on 2026-10-03:
**$2.00 input / $0.10 cached input / $10.00 output per 1M tokens**.

| Component | Tokens | Rate / 1M | Cost |
|---|---:|---:|---:|
| Fresh input | 854,883 | $2.00 | $1.7098 |
| Cached input | 41,989,427 | $0.10 | $4.1989 |
| Output | 136,621 | $10.00 | $1.3662 |
| **Total** | | | **$7.27** |

`OVERVIEW.md` states $7.28; that figure sums the three components *after*
rounding each to cents. The unrounded sum is **$7.2749**. Both round to $7.27–7.28
at two decimals, and the difference is presentational, not substantive.

**Cache dominance is the finding, not the total.** 57.7% of the spend is cache
reads. Billed at the cached rate, the 41.99M cached tokens cost $4.20; the same
volume uncached would have cost $83.98 — a saving of **~$79.78**, a **20×**
discount factor.

**Long-context surcharge checked, not applicable.** `gpt-6.1-sol` bills requests
above 272K input tokens at 2× input/cache and 1.5× output for the *whole* request.
The largest closing context recorded in the roadmap is 180K used of a 258K window
(Stage 9, "32% left"), so no request in this build crossed the threshold. Had one
crossed it, §3.1 would need recomputing — this is a live risk for longer future
sessions, not a settled fact.

**Not included: cache writes.** `gpt-6.1-sol` bills cache writes at $2.50/1M
(1.25× input). Session token counts in the roadmap record input, cached input and
output, with no separate cache-write column. If the platform billed them
separately, §3.1 is an undercount by an unknown margin. Codex subscription usage
is not itemised this way, so this is most likely a non-issue — but it is not
verified.

### 3.2 Other channels (cash, different providers)

| Channel | Provider / model | Cost | Note |
|---|---|---:|---|
| Planning & architecture | **OpenCode Go** — Kimi models | **~$5** | Half the plan quota |
| Ad-hoc review | Gemini Pro chat | **~$2–3** | Architecture consultation |

These are **not** OpenAI and **not** part of §3.1. Merging them would
double-count against any listener who hears "Codex" and "OpenCode" as the same
tool.

### 3.3 Sessions after the §13 snapshot — uncounted

`OVERVIEW.md` §13 covers the 8 sessions that produced stages 1–10. Work performed
after that snapshot is **not** in any figure above: the DT-90 MCP verification
(three real `codex exec` runs against the live stack), DT-91 and DT-92 planning,
and this accounting pass itself.

Estimate: **$0.2 – $0.6**. Derived from session shape (heavy prompt caching,
low reasoning effort), not from a recorded count. These are Codex sessions, so they
belong to the metered-equivalent column only and are **not** added to the cash row
in §1 — same reasoning as §3.1. They are excluded from both headline totals rather
than folded in, because the underlying token counts do not exist.

---

## 4. Provider API spend — measured

Three runs made real OpenAI calls. All figures from the machine-readable reports
in `docs/measurements/`, priced by `ModelCostCalculator` at the snapshot rates in
`pricing.py`.

| Run | Date | Model | Bulk | Triage | Total |
|---|---|---|---:|---:|---:|
| `sample_en.pdf`, EN→DE + DE→EN | 2026-10-02 | gpt-4o-mini | $0.000825 | **not recorded** | $0.000825 |
| FLORES matrix — pilot | 2026-10-03 19:10 | gpt-4o-mini | $0.001014 | $0.002977 | $0.003992 |
| FLORES matrix — pilot | 2026-10-03 19:10 | gpt-4o | $0.016942 | $0.008042 | $0.024985 |
| FLORES matrix — final | 2026-10-03 19:12 | gpt-4o-mini | $0.001025 | $0.003067 | $0.004092 |
| FLORES matrix — final | 2026-10-03 19:12 | gpt-4o | $0.016973 | $0.008193 | $0.025165 |
| **Measured total** | | | **$0.036779** | **$0.022279** | **$0.059058** |

**The pilot was not free.** It is a discarded run kept for provenance, and it cost
$0.028977 — roughly half of all measured provider spend. Reporting only the final
run would have understated real spend by that much.

### What the Compose database does *not* represent

The running stack's database (`/data/app.db`) shows 8 jobs and `$0.393` of
recorded job cost. **That is not money.** Every run there used
`LLM_PROVIDER=fake`; all 8 document analyses are `triage_status=degraded` with
zero tokens and zero cost, and the `$0.393` is the application *estimating* a
price against synthetic token counts produced by `FakeProvider`.

This is a live trap in the accounting: the database the operator can query looks
like a spending record and is not one. Only `docs/measurements/` holds real
provider usage.

---

## 5. Provider API spend — estimated where unrecorded

| Item | Estimate | Method |
|---|---:|---|
| Stage 9 triage, 2 documents × 3 attempts | **$0.006 – $0.018** | `sample-en.json` predates triage-usage instrumentation, so that spend is gone. Basis: the FLORES matrix priced one gpt-4o-mini triage attempt on a 20-block document at $0.003067. The Stage 9 documents have 12 blocks and 3 attempts each. The low end assumes several attempts failed without consuming tokens (429, timeout); the high end assumes all six were billed in full. |
| `make test-live`, 1–3 runs | **$0.0006 – $0.002** | Two live tests. `test_openai_provider_live_contract`: ~350 input / ~40 output tokens ≈ $0.00008. `test_live_triage_produces_plan_after_sdk_navigation`: an agent loop over 3 blocks, ~2 turns ≈ 2,400 input / 300 output ≈ $0.0005. |
| Ad-hoc manual provider calls while debugging | **< $0.005** | No records exist in the repository. Bounded by assumption, not evidence. |
| Compose, chaos script, MCP verification sessions, the Codex-driven `translate_file` run | **$0.000** | `LLM_PROVIDER=fake`; 0 tokens across all 8 analyses. |
| **Estimated total** | **$0.007 – $0.025** | midpoint **~$0.015** |

**Provider API, measured plus estimated: $0.066 – $0.084**, reported as
**~$0.07 – $0.08**.

---

## 6. Per-document economics

From the FLORES matrix: the *same* 20-block document, the *same* prompt, the same
supplied-reference chrF metric, two models.

| Figure | gpt-4o-mini | gpt-4o | Ratio |
|---|---:|---:|---:|
| Bulk translation cost | $0.001025 | $0.016973 | 16.6× |
| Triage cost | $0.003067 | $0.008193 | 2.7× |
| **Total per document** | **$0.004092** | **$0.025165** | **6.15×** |
| Triage ÷ bulk | **2.99×** | **0.48×** | |
| Triage input tokens | **28,228** | **2,589** | **10.9×** |
| Triage output tokens | 375 | 172 | 2.2× |
| Supplied-reference chrF | **71.37** | 70.84 | mini slightly better |

Three things follow, and only the first is comfortable:

1. **gpt-4o-mini's triage cost ~3× the translation it configures.** On the 12-block
   Stage 9 sample the bulk job was $0.000417 while triage for the same document
   would have been comparable or more.
2. **The cheaper model looped ~11× more.** mini consumed 28,228 input tokens in the
   triage agent where gpt-4o consumed 2,589 — same document, same tool schema, same
   navigation budget. The cost difference is not price-per-token; it is the agent
   taking roughly ten times as many turns to reach a plan.
3. **Upgrading mini→4o costs 6.15× and scores marginally *worse*** (70.84 vs 71.37
   chrF). On this document, model choice bought nothing.

Finding 2 is the operationally interesting one and it is direct evidence for the
turn-budget work in DT-92: a model that wanders is charged per turn, with no cap on
how far it wanders before the plan is accepted.

### Correction to an earlier figure

An earlier draft of this analysis claimed triage was "7.4× the bulk cost". That
figure divided the 20-block document's triage ($0.003067) by the *12-block*
sample's bulk cost ($0.000417) — two different documents. The correct same-document
ratio is **2.99×**, stated above.

---

## 7. What cost nothing, and why

The 417-test backend suite (plus the frontend suite) spent **$0.00** on API calls.

- `make test` runs against `FakeProvider` by policy (`AGENTS.md` rule 5).
- Every unmarked adapter test injects a stub client —
  `OpenAITriageAgent(client=object())` — so no real client is ever constructed.
- Independent review of the triage adapter "exercised the real SDK tool-calling
  loop with an **offline mock transport**" (`stage-6-triage-agent.md`), i.e. the
  SDK ran for real against a fake transport.
- Execution records for stages 2, 4, 6, 8, 9 and every task after them state
  "no live provider calls" or equivalent.

Of 417 tests, **2 are billable** (`@pytest.mark.live`), and only under
`make test-live`. Provider prompt caching, cache-read billing and the flaky
first-use `tiktoken` download are the only other metered side effects of a test
run, and none of them appear on the OpenAI API bill.

This matters for the headline: the expected fear ("test runs with real model
calls add up") does not apply here, and the isolation is by construction rather
than by luck.

---

## 8. Known gaps

| Gap | Effect | How to close |
|---|---|---|
| 41.99M cached tokens rest on one unreconciled source | The $7.27 aggregate has no independent corroboration | Per-stage cached-token capture in future sessions. The roadmap already warns its per-stage blocks sum to **1.9×** the session log and must not be used to back out cached volume. |
| Stage 9 triage spend unrecorded | §5 estimate range is 3× wide | Nothing retroactively; DT-80…85 instrumentation means future runs record it |
| `make test-live` run count unknown | $0.0006–0.002 band | Log runs, or accept the band as immaterial |
| Cache writes not counted | §3.1 may undercount | Requires itemised platform billing, not available from the CLI |
| Agent sessions after the §13 snapshot uncounted | §3.3 estimate only | Re-run the session accounting at submission time |
| Exact provider invoice never obtainable | All figures are application estimates of provider-*reported* usage | Structural. Ambiguous or uncheckpointed usage may never be known — stated in `DECISIONS.md` § Stage 9 live measurements |

On defense, defend the **shape** rather than the decimals: cache-dominated
development spend, provider API spend under 0.5% of total, triage costing ~3× the
bulk translation, and one measured model comparison showing a 6× price increase
with no quality gain.

---

## 9. Reproducing the measured figures

```bash
# Provider run reports (the only real usage records)
ls docs/measurements/
uv run python -c "import json;d=json.load(open('docs/measurements/2026-10-03-flores-en-de-matrix.json'));print([r['pipeline_usage'] for r in d['reports']])"

# Per-stage development token distribution
grep -n "Token usage" docs/assessment_context/roadmap.md

# Agent-session accounting and the cash/list-price distinction
sed -n '/## 13. Development cost/,$p' OVERVIEW.md
```

Application-side pricing, for re-deriving any cost from raw token counts:

```bash
uv run python -c "
from app.adapters.llm.pricing import ModelCostCalculator
c = ModelCostCalculator()
print('bulk mini', c.estimate('gpt-4o-mini', 1538, 1323))
print('triage mini', c.estimate_usage('gpt-4o-mini', 28228, 375, 0))
"
```

---

## 10. Maintenance

This document is a point-in-time accounting. Update it when any of these change:

- a new real provider run lands in `docs/measurements/`;
- triage usage instrumentation or a pricing snapshot is revised;
- the agent-session token accounting is re-run.

Keep the two bases separate, keep every estimate labelled as an estimate, and do
not fold the Compose database's `cost_usd` columns into provider spend — those are
fake-provider estimates and reading them as billing is the specific mistake this
document exists to prevent.
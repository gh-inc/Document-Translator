# DT-100 — triage convergence execution

Plan: [triage convergence](2026-10-03-triage-convergence.md).
Branch: `dt-100-triage-convergence`; baseline commit: `f8e32cd`.

## Scope and execution rulings

No public contract, schema, error catalog, tool-output bound, bulk path, cache,
chunker or renderer changes. Terminal exhaustion behavior remains fail-fast; the owner subsequently approved
separate triage and bulk model settings. Agents do not commit; root owns delivery.

Root owns task tracking, live measurements, documentation and checks. A delegated
implementer owns Phase 1 telemetry and offline regression tests. A separate
reviewer checked specification compliance and privacy; both verdicts are clean.
Two regression tests failed before the telemetry change and passed after it;
111 offline LLM adapter tests, scoped Ruff and backend mypy passed.

Measurements use an explicitly `@pytest.mark.live` temporary adapter harness,
not a deployment rebuild. It reads credentials through `Settings(_env_file='.env')`,
fixes the model to `gpt-4o-mini`, and uses `max_turns=16` before and after.
No production database or extraction is changed. The adapter's 60-second timeout
is retained. Each failed occurrence records its reported usage, with no retries.
Degraded rows denote adapter failure; the harness does not run the service fallback.
Only safe telemetry and aggregate classification/usage are retained, never source
text, keywords, reasoning or extracted terms. Network/thread I/O required sandbox
escalation. The planned provider-spend estimate was disclosed before running.

The baseline includes all five corpus documents and two additional complex-DOCX
runs, yielding the three repeated runs required by Phase 2. Prompt edits wait
until those measurements complete. Empty documents remain rejected. README and
`.env.example` will match the new 16-turn default, including direct adapter default.
Prompt caching is measured from reported cached tokens, never assumed.

## Evidence

Before evidence: [safe run records](../measurements/2026-10-03-triage-convergence-before.json).
The first five runs measure the corpus: 1/5 (20%) produced an accepted plan,
mean reported-usage cost $0.00244629, mean requests 14.0. This baseline already uses
16 turns, so the after comparison isolates outline/instruction changes; it does
not measure the effect of raising the deployment default from eight.

Three complex-DOCX runs all exhausted 16 turns. Read/search tallies were 2/14,
7/9 and 10/6; input/output tokens were 25,985/265, 28,706/277 and 35,515/286.
Their reported-usage cost estimates were $0.00243435, $0.00268650 and $0.00337725.
The PDF failures also spent many calls searching. This supports correcting the
instructions and giving initial context, but keyword lengths cannot identify
semantics: D1's specific absent-tool search hypothesis is not confirmed by these
privacy-preserving records. Both repeated searches and some repeated read ranges
occurred. No natural convergence was observed for the repeated DOCX at 16 turns.

All seven before calls cost $0.01829520 in reported usage estimates. Provider billing
may include unreported usage; these are snapshot-price estimates, not invoices.
At this initial checkpoint, the after measurement was recorded below while
policy selection and final checks were pending; later sections record their results.

After evidence: [safe run records](../measurements/2026-10-03-triage-convergence-after.json).

| File | Before status | Before requests | Before cost USD | After status | Domain | Terms | Input/output tokens | After requests | After cost USD |
|---|---|---:|---:|---|---|---:|---|---:|---:|
| sample_en.pdf | ok | 6 | 0.00080250 | degraded | general | 0 | 18339/246 | 16 | 0.00193845 |
| platon-complex.pdf | degraded | 16 | 0.00257415 | ok | philosophy | 4 | 44558/235 | 7 | 0.00402150 |
| platon-gliph.pdf | degraded | 16 | 0.00399420 | degraded | general | 0 | 121761/283 | 16 | 0.01004355 |
| platon-complex.docx | degraded | 16 | 0.00243435 | degraded | general | 0 | 113837/277 | 16 | 0.00938895 |
| platon-gliph.docx | degraded | 16 | 0.00242625 | degraded | general | 0 | 110107/265 | 16 | 0.00906225 |

The changed prompt again yielded 1/5 (20%) accepted plans, with mean 14.2
requests and $0.00689094 reported-usage cost per document. The accepted document
changed from the simple sample to complex PDF; this small, single-pass comparison
shows no success-rate improvement. Cost increased about 2.82 times. The outline
is bounded but repeatedly supplied as conversation context; observed cache hits
do not make it free. No representative success rate or cost share is claimed.
Bulk was not measured on this corpus, so current triage share of total spend is
unmeasured. The earlier FLORES ratio is a different corpus and cannot fill that gap.

The Phase 4 owner gate was presented after the mini measurement. The plan's low-success branch suggests gpt-4o,
but existing OPENAI_MODEL is shared with bulk: changing its default affects both.
The owner was presented that consequence and the option to keep mini or separate
the internal triage setting. The owner subsequently approved separate models; the implementation and its
measurements are recorded below.

Privacy verification: every captured live telemetry event passed an exact field
allowlist. Neither text/snippets nor keyword values/terms/reasoning are retained
in measurement artifacts. Direct adapter capture replaces the plan's Compose
log grep; no updated deployment/log completeness claim is made.

## Initial acceptance before the owner decision

Full `make test`: 676 passed, 2 live tests deselected. `make lint`: clean,
177 files formatted. `make typecheck`: clean, 61 source files. These three
checks passed on a task-only snapshot of baseline plus DT-100 files, excluding
concurrent MCP modifications. The first working-tree full suite also passed.
Phase 3 focused suite: 123 passed; independent reviewer scoped suite: 65 passed.
Independent implementation, privacy, arithmetic and documentation verdicts are
clean. The stale pending-measurement sentence found by review was corrected.

At this checkpoint Phases 1, 2, 3 and measured verification were complete, while
Phase 4 awaited the owner. No commit was made until that decision. The owner has
since approved the separation documented below. Concurrent MCP files remain
outside this delivery's file list.

## Agent token accounting — checkpoint before owner decision

Source: local Codex session `event_msg/token_count/info/total_token_usage` records.
Root session has no usage before the task request; use zero as its baseline.
Child sessions are fresh task-scoped sessions linked to this root. Input includes
cached input; output is the recorded output total, with reasoning already included.
This checkpoint excludes subsequent messages and future policy/delivery work,
so it is not a completed-task total. It is separate from live provider usage.

| Agent | Input tokens | Output tokens |
|---|---:|---:|
| /root | 6,829,449 | 16,455 |
| /root/review_telemetry | 274,241 | 1,841 |
| /root/review_final | 1,297,631 | 4,186 |
| /root/telemetry | 1,615,292 | 5,398 |
| /root/outline | 1,301,013 | 5,859 |
| **Total checkpoint** | **11,317,626** | **33,739** |

## Owner-selected policy and separate-model validation

The owner explicitly selected independent triage/bulk models and requested Luna6
or 5.6 checks. TRIAGE_MODEL defaults to gpt-4o; OPENAI_MODEL retains gpt-4o-mini
for bulk and glossary. Both real and fake triage use the independent triage
setting, preserving usage model identity. Terminal exhaustion is still terminal;
no retry budget, public contract or schema changed. The selected separation
avoids making all bulk translation use gpt-4o. Offline model-routing tests prove
that changing bulk does not change triage and vice versa.

[gpt-4o evidence](../measurements/2026-10-03-triage-convergence-gpt4o.json):
5/5 files yielded accepted plans, 3.8 mean requests, $0.03404600 mean reported
usage cost, $0.17023000 total. All reads/telemetry are checked by the same privacy
allowlist. Higher triage spend buys observed convergence here; no representative
quality or success-rate claim is made. Bulk share remains unmeasured.

The read-only Models API confirms both exact requested Luna IDs are available
with the configured key. Official model pages supply short-context snapshot
pricing, and GPT6 Luna requires reasoning_effort=none for Chat Completions tool
calling. SDK source was inspected before adding narrowly scoped compatibility.

Reproduction harness (marked live):
[Python source](../measurements/2026-10-03-triage-convergence-harness.txt).
Copy it to /tmp/dt100_measure.py and run from the repository root with
DT100_PHASE=<label>, DT100_MODEL=<model>, PYTHONPATH=. and
`.venv/bin/pytest -c pyproject.toml /tmp/dt100_measure.py -m live -q -s`.
Baseline used the pre-outline adapter and fixed mini before TRIAGE_MODEL existed;
that historical measurement requires the Phase 1 code, not the final adapter.

## Requested Luna evaluation

Both exact IDs passed five real corpus analyses with the final adapter:
[Luna6 records](../measurements/2026-10-03-triage-convergence-luna6.json),
[Luna5.6 records](../measurements/2026-10-03-triage-convergence-luna56.json).

| Model | Accepted plans | Mean requests | Mean service cost estimate USD | Mean including measured cache-write premium USD |
|---|---:|---:|---:|---:|
| gpt-4o-mini, corrected prompt | 1/5 | 14.2 | 0.00689094 | not captured |
| gpt-4o | 5/5 | 3.8 | 0.03404600 | not applicable to this snapshot |
| gpt-6-luna | 5/5 | 3.8 | 0.000861388 | 0.000998178 |
| gpt-5.6-luna | 5/5 | 4.0 | 0.001821832 | 0.002089002 |

Luna6 and Luna5.6 reported nonzero cache-write tokens. The service estimate uses
uncached/cached/output rates only; the experimental adjusted estimate adds
0.25 times the input rate for each reported cache-write token because those tokens
were already charged once as uncached input. Per-request input maxima were
7,173 and 6,977, below the documented 272K premium threshold. This does not cover
regional/Flex/Batch adjustments, and no invoice-total claim is made.
No public usage model or database field is changed to collect these additional
experimental counters. The harness wraps internal usage extraction and retains
only numeric detail; the service's cache-write billing gap is explicitly disclosed.

Installed SDK wire serialization was tested for both exact Luna IDs: reasoning
is disabled, max_completion_tokens=2000 is supplied via extra_body, legacy
max_tokens is omitted; gpt-4o/mini retain their existing request fields. Luna is
an explicit TRIAGE_MODEL option, while the owner-selected split defaults stay
TRIAGE_MODEL=gpt-4o and OPENAI_MODEL=gpt-4o-mini. Testing candidates did not authorize
an automatic default switch. Plans were accepted but classification accuracy,
terminology recall and downstream translation quality were not measured.

Official sources checked 2026-10-04:
[GPT6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[GPT5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna).
The two Luna runs add $0.01543590 in measured cache-write-adjusted estimates.
Combined known evaluation spend, including the mini and gpt-4o stages, is
$0.23841580, subject to the stated accounting gaps.

## Final acceptance and delivery

Final task-only `make test`: **685 passed**, 2 live tests deselected;
`make lint`: clean, 177 files; `make typecheck`: clean, 61 source files.
The task-only snapshot is baseline f8e32cd plus the scoped DT-100 changes,
excluding concurrent MCP modifications, including their README/architecture hunks.
Final production Python files and config match the verified snapshot byte for
byte. The implementer's sandbox-only test timeout experiment was removed to
restore the exact already-verified wire-test version. Existing Pydantic register
warnings also appear in the new actual-wire schema tests; no warning is suppressed.

Independent final reviewer confirmed model routing, request-wire compatibility,
all measurement/privacy arithmetic and documentation. Stale status wording found
by review was corrected. The final owner-selected behavior is separate models,
gpt-4o triage default, mini bulk default, terminal exhaustion unchanged, and both
requested Luna candidates explicitly selectable after successful live checks.
All authorized implementation and verification are complete; scoped commits
include task-hash backfill. No merge, push, deployment or production-DB change.

## Agent token accounting — final delivery snapshot

Implementation commit: `3a919c0`. A separate documentation commit backfills the
implementation hash and this final accounting snapshot. No amend, rebase or push.

This snapshot sums the root's task session and six fresh task-scoped child
sessions (one reviewer was reused). Input includes cached input across requests;
reasoning output is already included in output. It covers both task turns,
implementation, delegation, reviews, measurements orchestration and implementation
commit. It excludes subsequent documentation-commit/final-response tokens that
cannot be recorded inside their own result. Live provider tokens are separate.
Raw local token_count events are the source; counts are not estimated.

| Agent | Input tokens | Output tokens |
|---|---:|---:|
| /root | 16,060,462 | 35,665 |
| /root/review_telemetry | 274,241 | 1,841 |
| /root/review_final | 3,348,437 | 10,842 |
| /root/telemetry | 1,615,292 | 5,398 |
| /root/luna_compat | 1,828,245 | 6,292 |
| /root/outline | 1,301,013 | 5,859 |
| /root/split_models | 908,603 | 5,311 |
| **Final delivery snapshot total** | **25,336,293** | **71,208** |

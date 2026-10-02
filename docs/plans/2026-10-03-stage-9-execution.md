# Stage 9 execution record

Date: 2026-10-03 (Europe/Kyiv). Based on the user-approved local plan
`docs/plans/2026-10-02-stage-9-e2e-chaos-observability.md`. The original plan and
pre-existing approval notes remain user-owned working-tree changes.

## Ownership and delivery

| Tickets | Owner | Result |
| --- | --- | --- |
| DT-58, DT-59 | Container/chaos agent | Non-root multi-stage image, three processes, SQLite CLI, cached tokenizers, isolated restart proof and operator notes |
| DT-60 | Readiness agent | Persistence-only stale lease query, thin health service, six API readiness tests |
| DT-61 | Measurement agent | Explicit live CLI, full pipeline, supplied-reference/back-translation chrF, literal preservation, durable usage queries |
| DT-62 | Root | Real OpenAI sample run and machine evidence; explicit limits on total bill and population p95 |
| DT-63 | Root | Compose/local README, symptom-to-command runbook and frontend offline CI gates |
| DT-64 | Root + independent reviewers | Integration, acceptance, review and scoped commits with task hashes |
| DT-65 | Measurement agent, root acceptance | MCP completed-download permissions for host readability |

Agents used separate file ownership; no agent committed or changed public
models, endpoints, tools, or database schema. Existing user files are preserved.
The root remains on `stage-9-e2e-chaos-observability`; no merge or push is part
of this delivery.

## Rulings and corrections

- New attempts after restart are legitimate for both previously pending and
  interrupted chunks. Already-done chunks must retain their attempt counts.
  An interrupted provider invocation can have no durable outcome row at all.
- The script compares SQLite dates with `julianday`, scopes checks to an
  isolated document/database, kills while done/inflight/pending coexist, waits
  for both job and chunk leases, and cleans up unless `--keep` is supplied.
- Worker health checks PID 1; MCP checks the existing transport's 200/400
  response, since a sessionless GET returns 400. An initial probe accepting
  only 200 failed, was corrected, and the Compose scenarios were rerun.
- Readiness grace is `max(120, 2 * CHUNK_LEASE_SECONDS)` beyond lease expiry.
  An absent idle worker cannot be detected; a paused worker can report 503.
- Tokenizer data is baked into the image to avoid first-use network fetches.
- Independent Compose acceptance found that MCP downloads retained private
  mode 0600 with container UID 10001, preventing the host user from opening
  the result. DT-65 publishes the completed file as 0644 on its existing fd
  before atomic rename; copying remains private and contained.
- The measurement command polls document state after upload, wraps the full
  path in `asyncio.timeout`, and rejects fake/missing credentials before work.
- chrF uses the primary SacreBLEU implementation's effective-order means:
  [source](https://raw.githubusercontent.com/mjpost/sacrebleu/master/sacrebleu/metrics/chrf.py).
  The fixed non-identical unit fixture distinguishes it from pooling orders.
- Measurements cover rendered/extracted text. Unsupported DOCX table/header
  text is outside that extraction scope. Preservation compares token multisets
  literally, so localized date/number formatting can lower the result.

## Review

The readiness implementation was checked by root and six focused tests. A
separate measurement review found no blocking issues. A final independent
review covered Docker, Compose, chaos, readiness, measurement, CI and runbook.
Its proposed missing cost-cap variable was withdrawn after `rg` confirmed
`MAX_COST_PER_JOB_USD` in the current shared Compose environment. No critical
or important findings remain. The final reviewer also verified DT-65 file
publication, permissions, atomic rename and retained symlink protection.

## Verification evidence

- `make test`: 493 passed, 2 live tests deselected; two existing Pydantic
  `register` field warnings remain. SQLite/threaded tests ran with the sandbox
  restrictions lifted after normal restricted invocations stalled.
- `make lint`: clean; 143 files formatted.
- `make typecheck`: clean; 59 source files.
- Frontend: 66 tests passed, typecheck and production build passed.
- Docker image build passed; app import and cached tokenizer lookup passed
  under `--network none`; `sqlite3 --version` passed. Runtime UID is 10001.
- Offline chaos: one done + one inflight + pending work at SIGKILL; recovered
  to `done`, 10/10 chunks, 320/320 unique translations, output artifact present.
  Durable attempts went 1→10; the previously done chunk did not gain attempts.
  The isolated project, volume and temporary files were removed.
- Independent Compose acceptance passed: web, worker, MCP healthy; health,
  readiness, metrics and SPA routes return 200; REST PDF translated to three
  languages, each artifact parsed; repeated German job completed at zero
  recorded cost; MCP DOCX submitted/polled/downloaded and parsed on the host.
  Isolated containers, volume and temporary files were removed. Both the
  REST/MCP acceptance and full chaos script passed again from a clean local
  clone of committed runtime changes at `d1a4809`, with no `.env`, host venv,
  frontend node_modules, or working-tree modifications in the clone.

Live measurement succeeded with real OpenAI, model `gpt-4o-mini`, on the fixed
two-page/12-block sample PDF in both directions. Raw JSON is at
[measurement evidence](../measurements/2026-10-03-sample-en.json). Forward bulk
spend was $0.00041715; combined bulk spend $0.000825; chrF 85.2706; forward
number preservation 5/5. Reverse triage degraded. Triage spend, statistically
meaningful job p95 and parallelism comparison are explicitly unmeasured.
See the detailed scope in [DECISIONS.md](../../DECISIONS.md).

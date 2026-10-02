# Stage 8 execution record

Authority: the approved [frontend plan](2026-10-02-stage-8-frontend.md), the user's three refinements, and Architecture **Frontend**, **REST API surface**, and **Layering & the Document IR**.

Work stays in the existing checkout on `stage-8-frontend`, preserving the approved uncommitted Stage 8 documentation. `TEST_TASK.md` and `docs/roadmap.md` remain outside delivery. Root owns coordination, independent review, acceptance evidence, task records, and commits. Implementers do not commit or spawn agents.

| Ticket | Plan task | Owner / review |
|---|---|---|
| DT-47 | Scaffold | scaffold / review_scaffold |
| DT-48 | Typed client | api_client / review_client |
| DT-49 | Document status | document_status / review_document_status |
| DT-50 | Upload/readiness | upload_flow / review_upload |
| DT-51 | Jobs/SSE/retry/download | job_views / review_jobs |
| DT-52 | History | history / review_history |
| DT-53 | Branding/layout | branding, root SVG acquisition / review_branding |
| DT-54 | Static serving | spa_serving / review_spa |
| DT-55 | Development workflow | workflow_docs / review_workflow |
| DT-56 | Acceptance | root and browser_acceptance |
| DT-57 | Integration/delivery | root and final independent review |

Implementation agents have scoped file ownership and work sequentially. Reviews check both specification and quality. Shared App wiring is limited to each task's own routes or final shell; History reuse required a small callback addition to JobCard.

## Decisions and corrections

- Use a custom 404 exception handler for SPA navigation, overriding the plan's original catch-all approach as requested. `/api` and `/api/...` retain safe JSON errors; missing static resources and non-navigation methods retain 404.
- MIME must match `.pdf` or `.docx`, in addition to the 50 MiB limit. Backend upload validation remains authoritative.
- Final independent review reproduced HTTP/1.1 connection starvation with eight queued language cards: six persistent streams blocked ordinary API calls. The final correction limits streams to four per application page, polls excess active jobs with bounded GETs, and promotes waiting jobs as slots free. This adds read traffic for excess jobs while preserving all public endpoints and the supported target choices.
- SSE effects close their EventSource on cleanup and terminal state. Request revisions reject stale GET responses; connection identity rejects callbacks queued after close. React 18 Strict Mode never keeps more than one active connection per card.
- Catalogued API errors are normalized before display. Failed document status has no error fields in the approved response shape, so the UI offers safe generic recovery text without inventing diagnostics.
- Each submission retains its idempotency key for explicit retries. Readiness uses bounded GET polling and a deadline covering stalled requests; POST is issued after `extracted`, with no automatic POST readiness retries.
- Root downloads the official wordmark locally and changes only path fills from gray to white. Runtime assets remain local.
- History uses snapshot cards. Independent review found retry updated the card but left parent status filters stale; a callback now synchronizes the parent snapshots. Two regressions verify retry and subsequent refreshed status across filters; scoped re-review approved.
- Browser acceptance found real batch IDs contain a dot: the generic file-extension guard incorrectly rejected a direct batch refresh. Exact job/batch detail routes now allow dotted IDs while static namespaces retain 404s; GET/HEAD regression tests use the real deterministic ID shape.
- Patched Router 7 and Vitest 4 replaced initially vulnerable tooling. React 18 and Tailwind v3 remain the approved versions; the npm lockfile fixes the resolved graph. Initial npm audit reported zero vulnerabilities after the update.

## Verification evidence

Baseline backend: 421 tests passed, two live tests excluded; lint and mypy clean. Document-status coverage: 30 covering tests passed, including five new readiness cases.

Frontend after branding: 63 tests passed; typecheck and production build passed. Root Chromium layout checks at 1440, 375, and 320 pixels passed: no horizontal overflow, local SVG loaded, keyboard skip/focus worked, reduced motion respected, and no remote runtime requests. Root inspected wide and narrow screenshots. Full final checks and production browser flow are recorded below when complete.

No real OpenAI calls are made. Existing two Pydantic `register` warnings remain. Compose delivery stays Stage 9.

## Final acceptance

- `make test`: **476 passed**, two live tests deselected; two existing Pydantic warnings.
- `make lint`: clean; 136 files formatted.
- `make typecheck`: clean; 58 application source files.
- `npm ci --cache /tmp/document-translator-npm-cache --offline --no-audit`: clean lockfile install, 224 packages; cache avoids sandbox network and home writes.
- `npm run typecheck`, `npm test`, `npm run build`: passed; **66 tests in seven files**. Dependency audit previously reported zero vulnerabilities; no dependency versions changed afterward.
- Real Chrome with production build served by FastAPI, isolated WAL/files, and fake workers: **PASS**, 16 checks. PDF and DOCX each create two language jobs after extracted GET readiness, with exactly one creation POST per submission. Deterministic partial failure has a source-text warning and downloadable PDF; UI retry reaches done with positive cost and progressing chunks. Actual PDF/DOCX downloads parse and contain the expected text/prefix. History filters and job/batch links work. Direct job and dotted-batch reloads work. No browser page errors or external runtime requests. Narrow batch/job views have no overflow.

Browser evidence: `/tmp/document-translator-stage8-e2e-4Y9jXa/result.json` and six screenshots beside it. The QA script lives at `/tmp/document-translator-stage8-e2e.mjs`, using already-installed Playwright/Chrome without adding project dependencies. Initial harness PDF overflow produced an empty fixture; the fixture now asserts insertion and extraction before startup. A subsequent valid run exposed the dotted-batch bug, fixed and covered before the final passing run. Root visually inspected the narrow batch and wide history screens.

- Final Chromium HTTP/1.1 capacity probe: **PASS**, five checks with eight active jobs. Maximum four streams, ordinary GET available, all cards receive status/progress/cost, terminal completion promotes a waiting job, and navigation cancels streams/polling. No browser errors. Probe: `/tmp/document-translator-stage8-sse-capacity.mjs`.

Final independent review found one Important issue: aggregate SSE connection starvation. One consolidated correction and scoped re-review closed it, with no new Important/Critical regressions. All task reviews and the final integration gate passed. Delivery hashes are recorded in TASKS.md; the branch remains `stage-8-frontend`. Completion checks were repeated on 2026-10-03 without real provider calls.

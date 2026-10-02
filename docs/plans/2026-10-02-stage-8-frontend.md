# Stage 8 — Frontend Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a responsive React SPA for uploading documents, starting multi-language jobs, monitoring progress, and downloading results. Serve the production build from FastAPI on the same origin as the API.

**Architecture:**
- React + TypeScript + Vite + Tailwind CSS v3 (the approved `tailwind.config.js` configuration).
- Production API calls use relative `/api/...` paths. Vite proxies `/api` to FastAPI during development; no CORS layer is needed.
- A typed API client maps the backend's `{error_code, message, retryable}` envelope to UI states.
- After upload, the UI polls `GET /api/documents/{id}` until status is `extracted` or `failed`. It calls `POST /api/jobs` only once analysis is ready. It does **not** use repeated `POST /api/jobs` calls as a triage polling mechanism.
- Active jobs use SSE for progress, status, cost, and errors; the UI fetches current status on route entry/reconnect and closes EventSource connections when views unmount or jobs become terminal.
- Branding is based on `starkfuture.com`, not `getstark.co`: primary red `#FF1717`; black `#000000`; supporting dark neutrals `#242424` and `#1E1E1E`. `#242424` is the application's secondary/surface alias, not a verified official semantic "secondary" brand token.
- Download the official SVG locally and keep it under `frontend/public/`; recolor the local asset if required for contrast. No runtime hotlinking to images, fonts, or styles hosted by Stark Future.

**Tech Stack:** React, TypeScript, Vite, Tailwind CSS v3, npm with `package-lock.json`.

**Current State:**
- `frontend/` does not exist.
- FastAPI exposes upload, document triage status (to be added in this stage), job creation/status/retry/download, batches, SSE, and recent jobs.
- `app/api/main.py` does not yet serve a frontend build.
- `make dev` currently starts only FastAPI; frontend development will use a second terminal.

---

## Task 1: Scaffold the Frontend

**Files:**
- Create: `frontend/package.json`, `frontend/package-lock.json`
- Create: `frontend/vite.config.ts`, `frontend/tsconfig.json`
- Create: `frontend/tailwind.config.js`, `frontend/postcss.config.js`
- Create: `frontend/src/main.tsx`, `frontend/src/App.tsx`

Set scripts for `dev`, `build`, `typecheck`, and `test`. Configure Vite to proxy `/api` to `http://127.0.0.1:8000`. Add `react-router-dom` for routes `/`, `/jobs/:jobId`, `/batches/:batchId`, and `/history`.

Pin the resolved frontend dependency graph through `package-lock.json`. Use npm commands inside `frontend/`; do not hand-edit Python dependency pins.

**Exit criteria:** `npm ci`, `npm run typecheck`, and `npm run build` succeed on a clean frontend checkout.

---

## Task 2: Typed API Client and Error Mapping

**Files:**
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/errors.ts`
- Create: `frontend/src/api/client.test.ts`

Define TypeScript types matching the existing API schemas:
- `DocumentUploadResponse`
- `CreateJobRequest`
- `JobSummaryResponse`
- `BatchResponse`
- `ServerSentEvent`
- `ErrorResponse`

Implement methods for upload, document status, job creation, job status, retry, batch, recent jobs, and download. Parse non-2xx responses as the structured error envelope; never display raw response bodies or stack traces.

**Exit criteria:** client tests cover success, malformed/network failures, and catalogued error parsing.

---

## Task 3: Add the Document Status Endpoint

**Files:**
- Modify: `app/core/services/document_service.py`
- Modify: `app/api/routers/documents.py`
- Modify: `docs/api.md`
- Create or modify: `tests/api/test_documents.py`

Add a `DocumentService` read operation that returns the document record and persisted block count without exposing repositories to the router.

Expose:

```http
GET /api/documents/{id}
```

Return the existing `DocumentUploadResponse` shape (`id`, `filename`, `format`, `status`, `block_count`); return the catalogued 404 envelope for an unknown ID.

**Exit criteria:** the route reports `analyzing`, `extracted`, and `failed` states correctly, and status polling lives in the frontend client rather than in backend business logic.

---

## Task 4: Upload and Translation Flow

**Files:**
- Create: `frontend/src/features/upload/UploadPage.tsx`
- Create: `frontend/src/features/upload/UploadForm.tsx`
- Create: `frontend/src/features/upload/useTranslationSubmission.ts`
- Create: related component/hook tests

Build a drag-and-drop/file-picker upload flow for PDF and DOCX, with multi-select target languages.

Flow:
1. Client-side checks provide fast feedback for file extension and the 50 MiB maximum; server validation remains authoritative.
2. Upload with multipart `POST /api/documents` and show the returned `analyzing` state.
3. Poll `GET /api/documents/{id}` at a bounded interval until status is `extracted` or `failed`; allow cancellation and show a timeout action.
4. Only after `extracted`, call `POST /api/jobs` exactly once with a stable idempotency key retained for retries of that submission.
5. On `failed`, show the safe error and a concrete recovery action. If analysis times out, expose `POST /api/documents/{id}/retry-triage` as an explicit user action.

Do not poll `POST /api/jobs`, and do not retry it for `analysis_pending`.

**Exit criteria:** tests cover analyzing → extracted, analysis failure, user cancellation, timeout, and a single job submission after readiness.

---

## Task 5: Batch, Job, SSE, and Retry Views

**Files:**
- Create: `frontend/src/features/jobs/BatchPage.tsx`
- Create: `frontend/src/features/jobs/JobCard.tsx`
- Create: `frontend/src/features/jobs/useJobEvents.ts`
- Create: related component/hook tests

For each language job, show status, `done_chunks / total_chunks`, live cost, and available actions.

- Connect to `GET /api/jobs/{id}/events` while a job is active.
- Parse named SSE events (`status`, `progress`, `error`, `done`) and their JSON `ServerSentEvent` payload.
- Fetch `GET /api/jobs/{id}` on initial page load and after SSE reconnect.
- Close EventSource on unmount and once a job becomes terminal.
- For `completed_with_errors`, clearly state that untranslated blocks remain source text and offer retry via `POST /api/jobs/{id}/retry`.
- Provide download when the job is `done` or `completed_with_errors`.

**Exit criteria:** tests cover status transitions, progress/cost updates, SSE cleanup/reconnect, retry action, and terminal download state.

---

## Task 6: History View

**Files:**
- Create: `frontend/src/features/history/HistoryPage.tsx`
- Create: related component tests

Use `GET /api/jobs?limit=...` for recent jobs. Show status, language, progress, cost, and download/retry actions where applicable. Add a client-side status filter and link batches/jobs to their detail views.

**Exit criteria:** loading, empty, error, populated, and filtered states are tested.

---

## Task 7: Stark Future Branding and Accessible Layout

**Files:**
- Modify: `frontend/tailwind.config.js`
- Create: `frontend/public/branding/starkfuture-wordmark.svg`
- Create: `frontend/src/styles/`

Define theme aliases in `tailwind.config.js`:
- `primary: #FF1717`
- `secondary: #242424` (application surface alias; not claimed as an official Stark semantic token)
- `background: #000000`
- `surface: #1E1E1E`

Download the official wordmark from `https://starkfuture.com/next-assets/StarkFutureWordmark.svg` into the repository and make it legible against the dark background with a local SVG fill adjustment if necessary. Do not reference remote image or font URLs at runtime.

Ensure responsive layouts, visible keyboard focus, accessible form labels, status text not conveyed by color alone, and reduced-motion-friendly progress feedback.

**Exit criteria:** manual review on narrow/wide viewports and keyboard navigation; all runtime brand assets load locally.

---

## Task 8: FastAPI Static Serving with Safe SPA Fallback

**Files:**
- Modify: `app/api/main.py`
- Create or modify: `tests/api/test_frontend_static.py`

Serve `frontend/dist` in production, with static assets and SPA route fallback. Register all API/health/metrics routers first; register the SPA catch-all **last**.

The catch-all must explicitly reject `/api` and `/api/...` paths so API typos return the normal structured JSON 404 error instead of `index.html`. Unknown static asset paths should also return 404 rather than HTML.

**Exit criteria:** `/` serves the SPA; a client route such as `/history` serves `index.html`; `/api/nonexistent` returns a structured JSON 404; real API routes remain reachable.

---

## Task 9: Two-Terminal Development Workflow and README

**Files:**
- Modify: `Makefile`
- Create or modify: `README.md`

Keep `make dev` for FastAPI and add `make frontend-dev` for Vite. Document the approved two-terminal workflow:

1. Terminal 1: `make dev`
2. Terminal 2: `make frontend-dev`
3. Open the Vite URL; `/api` is proxied to FastAPI.

Document production serving through FastAPI. Compose-based `make up` remains a Stage 9 delivery check.

---

## Task 10: Verification

```bash
make test
make lint
make typecheck
cd frontend && npm ci && npm run typecheck && npm test && npm run build
```

Manual acceptance: upload a sample PDF/DOCX, observe analysis readiness without POST polling, start multiple language jobs, monitor SSE progress/cost, retry a partial failure, download output, and inspect history.

Optional if time allows: a browser-level Playwright test for the complete UI flow.

---

## Task 11: Backlog and Delivery

After the Stage 7 task IDs are assigned, use the next sequential IDs for Stage 8 work. Suggested logical groups:
- Frontend scaffold, client, and tests.
- Upload/document readiness workflow and tests.
- Batch/job/SSE/retry UI and tests.
- History, branding, and accessibility.
- Backend document-status endpoint and static serving.
- Two-terminal README workflow and full verification.

Do not commit until explicitly requested. On completion, update `TASKS.md` with actual IDs, statuses, and commit hashes.

---

## Critical Agent Reminders

1. **Document readiness first.** Poll `GET /api/documents/{id}`; issue one `POST /api/jobs` only after status is `extracted`.
2. **Safe SPA fallback.** Register the catch-all last and explicitly exclude `/api` and `/api/...` so API typos remain JSON 404s.
3. **Local branding assets only.** Use the downloaded SVG and local theme tokens; no hotlinking to Stark Future assets.
4. **Two dev terminals are approved.** Keep `make dev` for FastAPI and document a separate `make frontend-dev` startup.
5. **Structured errors and accessible states.** Render backend-safe messages, label retries, expose status and cost as text, and never treat color as the only status indicator.
# Stage 10 — Finalization & Submission Prep Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the documentation drift, put the reviewer's key answers directly in front of the reviewer, and leave the repository in a state where the assessment checklist can be walked top to bottom without guesswork.

**Architecture:**
- **Reviewer-first documentation.** `README.md` carries the architecture summary, the "who this is for" decision, and the agent-placement answer. `ARCHITECTURE.md` remains the deep reference; a deliberate, small duplication is accepted because reviewer convenience outweighs strict DRY in a submission document.
- **No contradictory state.** A document must never present a placeholder and its own replacement. `DECISIONS.md` §6 must not advertise "pending" measurements while §9 reports them.
- **Assessment context is separated from the product.** The original brief, the staged roadmap, and the Stage 9 plan move to `docs/assessment_context/` so the repository root contains only the product.
- **Human-owned git lifecycle.** No remote is created, nothing is pushed, and history is never rewritten. Repository publication and history compression are human activities.

**Tech Stack:** Markdown, GitHub Actions.

**Current State:**
- Stages 0–9 are implemented; `Dockerfile`, `docker-compose.yml`, `scripts/chaos-restart.sh`, and `scripts/measure_quality.py` exist.
- Live measurements are recorded in `DECISIONS.md` with explicit "not measured" entries.
- `DECISIONS.md` §6 still reads "_Pending implementation_" with a TODO list, contradicting §9.
- `README.md` has 11 operational sections but no inline architecture summary and no explicit agent-placement answer.
- CI runs lint/typecheck/tests (backend and frontend) but never builds the Docker image.
- `git remote -v` is empty.

---

## Task 1: Resolve the `DECISIONS.md` §6 / §9 Drift

**Files:**
- Modify: `DECISIONS.md`

The measured-numbers section currently opens with `_Pending implementation. To be reported on a fixed sample document:_` followed by a TODO bullet list, while §9 below it already reports real live measurements. A reviewer sees both and cannot tell which is current.

Rewrite §6 into a pointer section that:
- removes the "pending" wording and the TODO bullets;
- states that measurements are recorded in two places: renderer/layout measurements (§9 layout subsection or the Stage 3 subsection) and the live provider run (§9);
- keeps every existing "not measured" entry visible and explained — a stated gap reads as judgment, a contradicted placeholder reads as an oversight;
- points the reader at the exact subsection for each figure.

**Exit criteria:** no sentence in `DECISIONS.md` claims a measurement is pending while another section reports it.

---

## Task 2: Architecture Summary and Reviewer Answers in `README.md`

**Files:**
- Modify: `README.md`

Add a compact section near the top of the README. Do not restate `ARCHITECTURE.md`; give the reviewer a map plus the answers to the brief's explicit questions.

Required content:
1. **Process architecture in one view** — `web`, `worker`, `mcp` as two front doors onto one core, plus the shared `/data` volume. A short ASCII or Mermaid diagram is sufficient.
2. **Who this is for** — product and operations teams needing reliable business-document translation; explicitly not professional linguists.
3. **The three acceptance criteria** — resilience under `kill -9`, cost discipline via caching, multi-language independence — each in one or two sentences.
4. **A brief requirement-to-implementation table** — hard requirement → where it lives. This is the traceability answer in reviewer-readable form.
5. **A section whose header uses the exact phrase "Where an agent earns its keep"** (bold or heading). It must state plainly that the OpenAI Agents SDK is used for triage only, because triage must inspect an unknown document that does not fit in one context window and must make a judgment that shapes every downstream chunk; and that bulk translation deliberately avoids an agent loop because a fixed prompt mapped over N chunks needs determinism, parallelism, and predictable cost. This is one of the brief's explicit questions and must be answerable without opening another file.

Style: concise, skimmable, no marketing tone. Link to `ARCHITECTURE.md` for depth.

**Exit criteria:** a reviewer can answer "who is this for" and "where does an agent earn its keep" from the README alone.

---

## Task 3: Consolidated Testing Guide

**Files:**
- Modify: `README.md`

Present every verification command in one table: command → what it covers → whether it needs an OpenAI key, Docker, or running services.

Must include: `make test`, `make test-live`, `make lint`, `make typecheck`, frontend typecheck/test/build, `make up`, `./scripts/chaos-restart.sh`, and `scripts/measure_quality.py` (marked as live-only).

State plainly that the automated suite never calls the real provider, and that live tests are opt-in because they cost money.

**Exit criteria:** every verification command in the project appears in one place with its prerequisites.

---

## Task 4: Docker Build in CI

**Files:**
- Modify: `.github/workflows/ci.yml`

Add a job step that builds the image, so the "works anywhere" claim cannot silently regress through a local-only dependency.

Implementation notes:
- Run `docker build` on the repository root with the existing `Dockerfile`.
- Optionally validate the compose file with `docker compose config` to catch malformed service definitions without starting containers.
- Keep the job fully offline; no provider credentials, no cache-from-registry requirements.

Trade-off accepted: this lengthens CI. It is worth it because image buildability is a hard requirement and otherwise nothing would catch a broken `Dockerfile` until a reviewer runs it.

**Exit criteria:** CI builds the image on every push and pull request.

---

## Task 5: Assessment Context Packaging

**Files:**
- Move: `TEST_TASK.md` → `docs/assessment_context/TEST_TASK.md`
- Move: `docs/roadmap.md` → `docs/assessment_context/roadmap.md`
- Move: `docs/plans/2026-10-02-stage-9-e2e-chaos-observability.md` → `docs/assessment_context/2026-10-02-stage-9-e2e-chaos-observability.md`
- Modify: `TASKS.md`

Goal: keep the repository root about the product, while preserving the assessment record and keeping backlog references resolvable.

Notes:
- `TASKS.md` gains a pointer to the brief and roadmap at their new location.
- `AGENTS.md` contains no references to these files and needs no change.
- Historical execution records under `docs/plans/` mention the old paths. **Do not rewrite them.** They are accurate statements about what happened at that time; editing history-in-prose would itself be a form of falsification. Only live navigational links are updated.
- The Stage 9 plan sits apart from the other stage plans in `docs/plans/`; this is intentional per the approved instruction, since it was still uncommitted working material.

**Exit criteria:** root contains no assessment artifacts; every live link resolves.

---

## Task 6: Honest Final Verification Pass

**Files:**
- Create: `docs/plans/2026-10-02-stage-10-submission-verification.md`

Walk the assessment's "Before Submitting" checklist literally and record the result of each item, with evidence:

- Fresh clone works.
- `docker compose up --build` runs without errors.
- Upload a PDF → get a translated PDF back.
- Second format (DOCX) works.
- MCP server connects from a clean Claude Code / Cursor install using only the README.
- A document is translated end to end through MCP alone.
- Test suite passes.
- README includes a testing guide.
- `PROMPTS.md` and `DECISIONS.md` are complete and honest.
- No secrets committed.
- `.env.example` provided.

Rules:
- Report the actual observed result. If an item fails or could not be executed, write that down rather than checking it off.
- Include the commands run and the counts/versions observed.
- A verification table with honest failures is worth more than a clean-looking checklist.

**Exit criteria:** the verification record exists and every checklist item has a truthful status.

---

## Task 7: MCP Verification From a Clean Client

**Files:**
- Modify: `docs/plans/2026-10-02-stage-10-submission-verification.md` (results)

Confirm the README is sufficient on its own: configure the MCP server from a clean client using only the documented configuration, translate a sample document without opening the web UI, and record the outcome. If any step required knowledge not present in the README, that gap is a documentation defect — fix the README rather than noting it as a caveat.

**Exit criteria:** the MCP path is verified end to end using only README instructions.

---

## Task 8: Delivery Hygiene

**Files:**
- Modify: `TASKS.md`

- Record Stage 10 task IDs, mark them `done`, and backfill commit hashes.
- Confirm `.env` is untracked and `.env.example` holds placeholders only.
- Confirm `gitleaks` runs in pre-commit and reports no findings.

**Explicitly out of scope for the agent, by standing instruction:**
- Creating or configuring a git remote.
- Pushing to any host.
- `rebase`, `amend`, `squash`, or any history rewrite.
- Force-pushing.

These are human PR-preparation activities. The final commit-message history is left exactly as built so the human can squash it before publication.

**Exit criteria:** backlog complete, no secrets tracked, history untouched.

---

## Task 9: Final Verification Commands

```bash
make test
make lint
make typecheck
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
docker compose config
docker build .
git status --short
git log --oneline -5
```

**Exit criteria:** all commands succeed; `git status` shows no unintended leftovers; history is unchanged in shape (no new rebase/squash commits).

---

## Critical Agent Reminders

1. **Never contradict yourself in writing.** A pending-measurement note must not coexist with the measurement it supposedly awaits.
2. **Use the exact phrase "Where an agent earns its keep"** in the README so the reviewer finds the answer instantly.
3. **Do not rewrite historical execution records.** They describe what happened; only live navigational links are updated.
4. **No remote, no push, no history rewrite.** Not as an oversight — as a hard boundary.
5. **Report verification failures honestly.** A truthful failed item is deliverable; a falsely checked box is not.
6. **Accept the small documentation duplication** between README and ARCHITECTURE.md; reviewer experience wins here.
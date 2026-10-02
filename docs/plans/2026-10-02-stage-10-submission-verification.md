# Stage 10 submission verification

Observed on 2026-10-03 (Europe/Kyiv), executing
[the approved submission plan](2026-10-02-stage-10-submission.md).
The checklist below follows [Before Submitting](../assessment_context/TEST_TASK.md#before-submitting)
literally. Offline runs use FakeProvider; their output proves pipeline execution,
not linguistic quality. Existing real-provider figures remain in
[Stage 9 live measurements](../../DECISIONS.md#stage-9-live-measurements).
No additional live OpenAI translation calls are needed; existing Stage 9
figures are reused. The editor-client check is recorded separately below.

## Before Submitting

| Checklist item | Observed status | Evidence / limits |
| --- | --- | --- |
| Fresh `git clone` works | Passed for local clone | Isolated clean local clone at runtime HEAD `06354ce` in `/tmp/document-translator-stage10-clean`. No remote exists, so hosted-clone access is outside this check. Runtime code is unchanged by Stage 10. |
| `docker compose up --build` runs without errors | Passed | Isolated fake-provider project `dt-stage10-2f6871bb`; all three services healthy after `up --build -d --wait`. |
| Upload a PDF → get a translated PDF back | Passed offline | REST sample upload, analysis, job completion and download; parsed 5-page PDF, 1,708,473 bytes, with `[de]` translated markers. |
| Second format works | Passed offline | MCP sample DOCX submission, worker completion and download; parsed 10 paragraphs, 37,170 bytes, with translated markers. |
| MCP server connects from a clean Claude Code / Cursor install using only the README | Unverified: client account blocked | Existing Claude Code 2.1.287 was tested with an isolated strict README-style HTTP configuration. The escalated retry exited 1 with `Credit balance is too low`; the response was marked `is_error=true` despite its `success` subtype. No new French jobs appeared. A pristine install was not performed and Cursor was not exercised. Protocol success below does not certify this item. |
| A document can be translated end to end through MCP alone | Passed via protocol client | Four tools discovered over HTTP; `translate_file` → `check_status` → `download_result` completed for both PDF and DOCX without REST or web UI. PDF: 5 pages / 1,708,473 bytes. Downloads read successfully from the host output mount. This is separate from editor-client verification. |
| Test suite passes | Passed | `make test`: 493 passed, 2 live tests deselected, 2 existing Pydantic `register` warnings. Frontend: 66 tests across 7 files passed. |
| README includes a testing guide | Passed | Independent review confirmed the consolidated command/prerequisite table covers the plan's commands, including explicit live-only tests and measurements. |
| PROMPTS.md and DECISIONS.md are complete and honest | Passed with explicit measurement limits | Independent review confirmed measurement placeholders are removed and unmeasured billing, population p95, parallelism and reference-quality gaps remain. PROMPTS records delegation and rejected/corrected README output; its pre-existing user edits remain excluded from delivery. |
| No secrets committed | Passed for current history and tracked files | Pre-commit gitleaks all-files passed; redacted history scan covered 49 existing commits, no leaks. Final commit hook rechecks staged content. `.env` is ignored and untracked. |
| `.env.example` provided | Passed | Tracked example uses the `sk-...` placeholder and documents provider/storage settings; no real credentials. |

## Final commands

Commands run from the repository root. `UV_CACHE_DIR=/tmp/document-translator-uv-cache`
was used for local uv commands so cache writes remain isolated. `rtk` condenses
output without changing command behavior.

| Command | Observed result |
| --- | --- |
| `make test` | 493 passed, 2 deselected, 2 warnings; first run 59.90 s, repeated outside sandbox 54.08 s |
| `make lint` | Ruff check clean; 144 files already formatted |
| `make typecheck` | No issues in 59 source files |
| `npm --prefix frontend test` | 66 passed; 7 test files |
| `npm --prefix frontend run typecheck` | Exit 0 |
| `npm --prefix frontend run build` | Exit 0; Vite 6.4.3; 50 modules transformed |
| `docker compose --env-file /dev/null config --quiet` | Exit 0; no resolved credentials printed |
| `docker build -t document-translator:stage10-2f6871bb .` | Exit 0 from the clean clone |
| `uv run pre-commit run gitleaks --all-files` | Passed |
| `uv run pre-commit run --all-files` | Ruff, Ruff formatting, and gitleaks passed |
| Cached gitleaks from the hook pinned at v8.21.2: `detect --source . --redact --no-banner` | 49 commits scanned; no leaks; binary reports a build-time version placeholder |
| `git diff --check` | Exit 0 |
| `git status --short` / `git log --oneline -5` | Initial user change: PROMPTS.md only. Initial HEAD: 06354ce. Final delivery state will be recorded in TASKS.md. |

Versions observed: Python 3.12.2, pytest 9.1.1, FastMCP 4.0.10, pre-commit 4.6.2,
Docker 29.8.1, Compose v5.5.1, uv 0.12.21, Node v24.20.0, npm 12.0.2,
Claude Code 2.1.287.

## Clean-clone runtime and MCP checks

The runtime agent cloned the local repository into
`/tmp/document-translator-stage10-clean`; `git status --short` was empty there.
The exact clone command was:

```bash
git clone --no-hardlinks /home/alex/projects/POCs/document-translator /tmp/document-translator-stage10-clean
git -C /tmp/document-translator-stage10-clean status --short
git -C /tmp/document-translator-stage10-clean rev-parse HEAD
```

Compose used an isolated named volume, dedicated host MCP share and unused
ports: web 39513, MCP 52647. Its environment explicitly selected `LLM_PROVIDER=fake`,
`FAKE_FAIL_RATE=0`, `FAKE_LATENCY_MS=0`, `FAKE_FAIL_MODE=timeout` and
`MCP_HOST_SHARED_DIR=/tmp/document-translator-stage10-runtime-4e6fdb4c/mcp-files`.
No `.env` file or provider credentials were supplied. The startup command was:

```bash
docker compose --env-file /dev/null -p dt-stage10-2f6871bb up --build -d --wait --wait-timeout 180
```

With the test ports and dedicated share exported, the HTTP acceptance script
checked `/healthz`, `/readyz`, `/metrics`, `/` and `/history` (all 200), then
parsed actual worker-produced REST and MCP downloads. PDF and DOCX MCP tests
used the README's relative `input/` paths, polling and `output/` download recipe.
The exact host harness command was:

```bash
/home/alex/projects/POCs/document-translator/.venv/bin/python /tmp/document-translator-stage10-runtime-4e6fdb4c/runtime_acceptance.py
```

This temporary harness is not shipped with the repository; reproduce its
workflow using the README MCP recipe or the retained offline integration tests.
The REST job completed 1/1 chunks. PDF rendered text had 526 characters with
`[de]`; DOCX had 437 characters with `[fr]`. The built image digest was
`sha256:4fa18c9a1a49fab719cfaa8f061a08c36d7f5cd4f11a7d1f4c9b5ca45ecf4641`.

Sandbox Docker socket/local networking restrictions required rerunning these
checks with escalation; they were not application failures. Other running
containers were left untouched.

Cleanup completed with exit 0 using the same test ports/share settings:
`docker compose --env-file /dev/null -p dt-stage10-2f6871bb down --volumes --remove-orphans`.
Only that project's containers, network and named volume were removed.

## Isolated editor-client attempt

The README HTTP JSON was saved to a temporary `claude-mcp.json`, changing only
the URL port to 52647 for the isolated stack. The existing client was launched
with `--strict-mcp-config --mcp-config`, empty settings sources, no persisted
session and only the translator MCP tools allowed. Its instruction was to
translate `input/sample_en.pdf` to French, poll completion, download to
`output/`, and report the artifact. The budget was capped at $0.50; API-key
environment variables were removed from this client process.

The harness invoked this command from its isolated temporary directory:

```bash
claude --strict-mcp-config --mcp-config /tmp/document-translator-stage10-runtime-4e6fdb4c/claude-mcp.json --setting-sources '' --no-session-persistence --tools '' --allowedTools 'mcp__stark-translate__*' --max-budget-usd 0.50 --output-format json --print 'Use only stark-translate MCP tools: translate input/sample_en.pdf to French, poll check_status until done, then download_result to output and report the artifact.'
```

The quoted instruction summarizes the harness prompt; the flags and
configuration path are exact. The local Python launcher was
`/home/alex/projects/POCs/document-translator/.venv/bin/python /tmp/document-translator-stage10-runtime-4e6fdb4c/claude_client_smoke.py`.

The first sandbox attempt produced no output and was stopped at its wall-time
limit. One escalated retry returned `Credit balance is too low`; comparing
`list_recent_jobs` before/after found zero new French jobs. No editor end-to-end
success is claimed. Funding the Claude account and rerunning the README recipe
is the remaining external prerequisite. The independent reviewer found no
configuration defect in the README snippets.

## Scope and review

README, DECISIONS and ARCHITECTURE changes describe the implementation in “System architecture”,
“Guarantees”, “LLM provider & the agent question”, and “Testing strategy” of
ARCHITECTURE.md. No public contracts or runtime code change. CI adds an image
build on every push and pull request without invoking providers or publishing
an image; dependency/base-image downloads still need network access.

The brief and roadmap were already relocated by prior commits `d3e5089` and
`06354ce` (DT-66). This stage moves the Stage 9 plan as explicitly instructed,
retains historical prose path mentions, and verifies live navigation links.
The task IDs for this execution are DT-67–DT-75.

Review corrections restored the runnable Compose quickstart alongside the
complete command table, added PDF web delivery and crash-recovery requirement
rows, and invoked pre-commit through uv because it is installed in the local
virtual environment. Local Markdown navigation targets in README, TASKS,
DECISIONS and this record all resolve. Historical execution prose is unchanged.

Final review found that the architecture still described all ambiguous
provider billing as measured, contradicting its implementation and DECISIONS.
The architecture now reports known persisted bulk-attempt spend and explicitly
excludes unknown/uncheckpointed usage and triage billing. It also removes the
unsupported claim that a horizontal-scaling comparison was measured.
Cache-hit instrumentation and population-p95 wording were also aligned with
the documented gaps; these edits do not change the implemented cache or worker.

The user authorized commits. No remote was created, nothing was pushed, and
history was not rewritten. The pre-commit hook is installed and includes
gitleaks v8.21.2. Delivery uses a content commit followed by a task-hash backfill
commit, without amend or rebase.

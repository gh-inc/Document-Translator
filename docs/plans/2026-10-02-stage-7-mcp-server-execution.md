# Stage 7 execution record

Implemented the approved [Stage 7 plan](2026-10-02-stage-7-mcp-server.md) with
the user's atomic-triage and 45-second-deadline refinements. The controlling
architecture sections are “Layering & the Document IR”, “Data model (SQLite,
WAL)”, “Pipeline”, “REST API” and “MCP server”.

## Ownership and implementation

| Tickets | Owner/responsibility |
|---|---|
| DT-41 | Root: shared directory settings, containment and descriptor traversal |
| DT-42 | Agent: service/persistence recent query and thin REST collection route |
| DT-43–DT-45 | MCP agent: service composition, lifecycle, four tools, protocol/E2E tests |
| DT-44 | Triage agent: conditional service claim, shared locks, crash/cancellation recovery |
| DT-46 | Root: integration, documentation, HTTP verification, full checks and commits |
| Final review | Independent read-only agent, followed by scoped regression review |

Implementers read the architecture and inspected installed third-party source.
They did not commit or spawn additional agents. Work used a new
`stage-7-mcp-server` branch in the existing checkout to preserve the user's
uncommitted documentation and installed environment.

MCP composes the existing services and ports directly. SQL stays inside
persistence; no API imports or HTTP self-calls. Recent-job limits default to
10 and clamp to 1–100, with stable newest-first ordering.

The service commits a conditional document status claim before scheduling.
An advisory lock on the shared local data filesystem distinguishes a live owner
from abandoned `analyzing` work without a schema migration. Locks are released
on cancellation/process death; successful analysis and plans already used by
jobs are reused. Triage DB scopes close before provider calls. Shared upload
locking prevents losing duplicate ingestion from deleting the winning artifact.

Runtime-owned readiness continues after the tool's maximum 45-second wait.
`check_status` accepts the returned document ID in its existing string parameter;
once extracted, the caller repeats idempotent `translate_file` to enqueue jobs.
Errors are explicit safe catalogued models. FastMCP's union/list output uses
`structuredContent.result`. Downloads stay beneath the dedicated shared mount.
Compose wiring remains Stage 9, as allowed by the approved plan.

## Review findings resolved

- Cancelled-before-start tasks and failed REST response sends could skip task
  cleanup. Prepared handles now have caller-owned cleanup through runtime claim
  tracking and request-scoped dependencies.
- A successful persisted analysis with inconsistent `analyzing` status was
  blocked by the claim condition. The service repairs readiness atomically
  under exclusive ownership without invoking a provider.
- Resolving paths before reopening them allowed filesystem substitutions.
  Reads now use anchored directory descriptors and `O_NOFOLLOW|O_NONBLOCK`,
  then validate the opened regular file. Downloads retain their exclusively
  opened temporary descriptor, verify its identity and publish relative to
  the held directory. Regression tests cover substitutions and FIFO inputs.
- SQLite cancellation cleanup could extend the deadline beyond its configured
  limit. The tool races separately owned readiness against its timer. An actual
  locked-writer test confirms a timely response and eventual extraction after
  writer release, without a new submission.

Final independent review has no remaining findings. POSIX advisory locking and
directory descriptor operations target the existing Linux/local-WAL deployment.
The data directory beside SQLite must be shared and writable by both processes.

## Verification and limits

- `make test`: **421 passed**, 2 live tests deselected; two existing Pydantic
  `register` shadow warnings.
- `make lint`: clean, 130 files formatted.
- `make typecheck`: clean, 57 source files.
- Independent MCP suite: 31 passed.
- Real local HTTP smoke using `python -m app.mcp_server` and a separate fake
  worker: discovered four tools, submitted sample PDF, observed done, downloaded
  valid PDF, listed recent jobs and rejected an outside path. Repeated after
  final fixes. The automated protocol E2E also checks translated PDF content.
- Concurrent process ownership and SIGKILL/restart use real subprocesses;
  failed-response and pre-start cancellation, immutable analysis, duplicate
  ingestion and real SQLite writer contention have regression coverage.
- Installed Claude Code command syntax and official editor configuration were
  checked. A manual clean-config Claude Code session was attempted with temporary
  MCP configuration, only the translation tools enabled, no session persistence,
  and a $1 spend bound. The CLI returned an error result; it did not validate
  editor translation. Authenticated Claude Code/Cursor verification remains
  unverified. README contains the exact configuration and three-step recipe.

Threaded SQLite and local networking checks needed execution outside the sandbox.
Project LLM verification used fake providers; no live OpenAI tests ran.
Unrelated user changes in architecture/decision/prompt documents, assessment file
and roadmap remain outside the delivery commit.

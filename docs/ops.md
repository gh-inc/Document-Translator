# Container Operations

## Compose services and health

`docker compose up --build` starts three processes from the same image. The
web service initializes `/data/app.db` before accepting requests. The worker
and MCP services wait for the web `/healthz` check and mount the same named
`/data` volume, so they use the same database, uploads, and outputs.

`web` is healthy when its HTTP liveness endpoint responds. The worker has no
HTTP listener, so its Docker healthcheck checks that PID 1 is alive. That
detects a stopped process; it does not prove that a job is progressing. The
MCP process also has no separate health endpoint. Its check opens the installed
FastMCP `/mcp` SSE route and accepts HTTP 200 or FastMCP's HTTP 400 response
for a GET without an initialized session. These container checks are
liveness checks and do not replace application `/readyz`, which checks
database and storage access and returns 503 when an `inflight` chunk lease is
stale by more than `max(120 seconds, 2 × CHUNK_LEASE_SECONDS)`. An idle
database with no `inflight` chunks reports ready, so this heuristic cannot
prove that a worker is alive. A legitimately long-running chunk past its
lease grace is reported as not ready until it recovers.

Compose defaults to `LLM_PROVIDER=fake`, so the built stack can translate
without calling an external provider or supplying an API key. Set
`LLM_PROVIDER=openai` and provide `OPENAI_API_KEY` for real provider calls.

MCP reads and writes files only under `MCP_HOST_SHARED_DIR` on the host, mounted
as `/mcp-files` in the container. The container runs as UID 10001; give that
dedicated host directory write permission for UID 10001 (or an ACL) if MCP
needs to save downloads there. Prefer ownership (`sudo chown -R 10001:10001
mcp-files`) over `chmod 0777`: artifacts publish at mode 0644, so the host needs
read access only. A missing `output/` directory is healthy — the service creates
it under its own UID on first use — so a wrongly-owned one is best removed with
`sudo rm -rf mcp-files/output`. Run `make mcp-share-check` for a read-only report
of ownership per directory plus the exact remedy; it never modifies anything.

Only `download_result` is affected by an unwritable share, because
`translate_file` and `check_status` read rather than write. The MCP server logs
`mcp_shared_dir_not_writable` once at startup, and `download_result` answers
`shared_dir_unavailable` with `retryable=false`, so a client stops rather than
looping on a fault that no client action can resolve. Completed downloads publish
with mode 0644 so the host user can open files created by container UID 10001;
temporary copies remain private until publication. The rest of the host
filesystem is not mounted.

## Restart-under-chaos verification

Run from the repository root with Docker Compose and `python3` available:

```bash
./scripts/chaos-restart.sh
```

The script builds an isolated compose project with its own named data volume,
starts the fake provider with one translation at a time and a deliberate delay,
and uploads a generated DOCX containing enough text for several chunks. It
waits until SQLite shows a `done` chunk, an `inflight` chunk, and a `pending`
chunk, then kills the worker with `SIGKILL`. The worker restarts after the
interrupted chunk lease expires and the script checks durable rows through the
`sqlite3` CLI inside the container.

The proof is scoped to the submitted document. It checks that every document
block has one committed translation, all chunks and the job finish, and the
output file exists. It snapshots per-chunk attempt counts and rejects any new
attempt for a chunk that was already `done` at the kill point. Attempts may
grow for a chunk that was `inflight` or `pending`; a provider call interrupted
before its result is committed can be repeated. This is at-least-once provider
invocation behavior, while committed translations remain unique and are not
requested again.

`chunk_attempts` records calls after an outcome is known and persisted. A
process killed during a provider request can leave that particular invocation
without an attempt row, so the table measures recorded outcomes and cannot
prove whether the remote provider processed that interrupted request.

The script removes its compose project, named volume, and temporary files on
success or failure. Pass `--keep` to retain them for inspection; it prints the
project name and temporary directory. Clean up a retained run with:

```bash
docker compose --project-name <printed-project-name> --file docker-compose.yml down --volumes
```

The chaos run chooses unused host ports and overrides the default lease with a
shorter lease to keep the verification practical. Production compose uses the
configured `CHUNK_LEASE_SECONDS` (60 seconds by default).

## Translation cache accounting

The UI cached percentage and `/metrics` counters `cache_hits_total` and
`cache_misses_total` read the same durable job columns, `cache_hit_blocks` and
`cache_miss_blocks`. The unit is block lookups, separate from chunk progress;
percentage is hits / (hits + misses), hidden before any lookup. Window hit rate
is the corresponding ratio of deltas between scrapes. Retry/re-delivery can
count another lookup; a crash before the counter transaction omits that lookup.
Repeated scrapes do not add counts. No source text is a metric label.

Cache identity covers target language, model, prompt version, glossary and the
entire analysis plan, then exact source text via SHA-256. Neighbouring context
is excluded. A deploy from the legacy block-ID cache drops those cache rows;
jobs and attempt accounting remain. Previously done chunks retain their status,
so an in-flight job can finish as `completed_with_errors`, retaining source text
for those missing translations. Use the normal Retry action to requeue these
cache misses and retranslate them after the upgrade. Existing completed output
files remain available.

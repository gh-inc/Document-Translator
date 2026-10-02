# Worker process

Run the worker after documents, extracted blocks, jobs, and chunk-block links
have been persisted:

```bash
LLM_PROVIDER=fake \
DATABASE_PATH=/tmp/document-translator/app.db \
UPLOAD_STORAGE_PATH=/tmp/document-translator/uploads \
OUTPUT_STORAGE_PATH=/tmp/document-translator/out \
uv run python -m app.worker
```

The worker initializes SQLite WAL and the schema before claiming work. It
processes one job at a time. `MAX_CHUNK_CONCURRENCY` (default 8) bounds parallel
chunk execution within that job. SIGINT/SIGTERM stop new job claims and allow
the current job to finish. Forced cancellation leaves leases for recovery.

`JOB_LEASE_SECONDS` and `CHUNK_LEASE_SECONDS` default to 60;
`HEARTBEAT_INTERVAL_SECONDS` defaults to 10 and must be shorter than both leases.
Heartbeats continue while rendering. A restarted worker waits for outstanding
leases to expire, recovers inflight chunks, and skips committed cache results.
If a chunk lease expires while its job lease remains valid, recovery waits for
the job lease to expire; this can delay work by up to that lease duration.
Retryable provider failures use exponential backoff with jitter;
`MAX_CHUNK_ATTEMPTS` defaults to 4, including persisted attempts before restart.

`MAX_COST_PER_JOB_USD` defaults to 2.00. Each call reserves an estimate under an
async lock; actual reported token usage settles the estimate. Calls with unknown
usage conservatively retain their local reservation. Durable job costs contain
known billed usage from attempts; an ambiguous provider timeout can still incur
unreported spend. Reservations use a conservative UTF-8 byte estimate with prompt
overhead and output expansion allowance, so the cap may stop work early.

The final status comes from cache coverage: all blocks cached means `done`;
missing translations mean `completed_with_errors` and source-text fallback.
Rendering/storage failures produce safe `render_failed` errors. Public domain
models, repository ports, and database schema are unchanged.

The default provider is `openai`; configure its key through application Settings.
Use `LLM_PROVIDER=fake` for offline execution. Tests use FakeProvider exclusively.
This stage supplies execution of pre-enqueued jobs; upload, enqueue, REST, and
triage services are delivered in later stages. Missing persisted analysis uses
the worker's degraded source-side plan.

Execution ownership is checked before cache checkpoints and artifact publication.
Filesystem publication and lease checks cannot be one atomic transaction through
the existing ports; simultaneous workers during lease loss can still race to
publish an artifact. Horizontal worker scaling remains outside the project scope.

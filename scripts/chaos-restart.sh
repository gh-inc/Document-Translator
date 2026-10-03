#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    printf 'Usage: %s [--keep]\n' "${0##*/}" >&2
}

keep_stack=false
case "${1:-}" in
    "") ;;
    --keep) keep_stack=true ;;
    *) usage; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
    usage
    exit 2
fi

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repo_dir/docker-compose.yml"
project="doc-translator-chaos-${$}"
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/${project}.XXXXXX")
sample_file="$tmp_dir/chaos-sample.docx"
mkdir -p "$tmp_dir/mcp-files"
chmod 0777 "$tmp_dir/mcp-files"

compose=(docker compose --env-file /dev/null --project-name "$project" --file "$compose_file")
export LLM_PROVIDER=fake
export FAKE_FAIL_RATE=0
export FAKE_LATENCY_MS=5000
export FAKE_FAIL_MODE=timeout
export MAX_CHUNK_CONCURRENCY=1
export CHUNK_LEASE_SECONDS=12
export HEARTBEAT_INTERVAL_SECONDS=2
export JOB_LEASE_SECONDS=12
export MCP_HOST_SHARED_DIR="$tmp_dir/mcp-files"
read -r WEB_PORT MCP_PORT < <(python3 - <<'PY'
import socket

ports = []
bound_sockets = []
for _ in range(2):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    bound_sockets.append(sock)
    ports.append(sock.getsockname()[1])
print(*ports)
for sock in bound_sockets:
    sock.close()
PY
)
export WEB_PORT MCP_PORT
web_url="http://127.0.0.1:$WEB_PORT"

cleanup() {
    local exit_code=$?
    if [ "$keep_stack" = false ]; then
        "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
        rm -rf -- "$tmp_dir"
    else
        printf 'Keeping compose project %s and files in %s\n' "$project" "$tmp_dir" >&2
    fi
    exit "$exit_code"
}
trap cleanup EXIT

die() {
    printf 'chaos-restart: %s\n' "$*" >&2
    exit 1
}

wait_for_services() {
    local deadline=$((SECONDS + 240))
    local service container state
    while (( SECONDS < deadline )); do
        local all_healthy=true
        for service in web worker mcp; do
            container=$("${compose[@]}" ps -q "$service")
            if [ -z "$container" ]; then
                all_healthy=false
                break
            fi
            state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
            if [ "$state" != healthy ]; then
                all_healthy=false
                break
            fi
        done
        if [ "$all_healthy" = true ]; then
            return 0
        fi
        sleep 2
    done
    "${compose[@]}" ps >&2 || true
    die 'timed out waiting for healthy web, worker, and MCP services'
}

db_query() {
    "${compose[@]}" exec -T web sqlite3 -noheader /data/app.db "$1"
}

wait_for_job_state() {
    local job_id=$1
    local deadline=$((SECONDS + 180))
    local state counts done_count inflight_count pending_count
    while (( SECONDS < deadline )); do
        state=$(db_query "SELECT status FROM jobs WHERE id='$job_id';" | tail -n 1)
        counts=$(db_query "SELECT SUM(status='done'), SUM(status='inflight'), SUM(status='pending') FROM chunks WHERE job_id='$job_id';" | tail -n 1)
        IFS='|' read -r done_count inflight_count pending_count <<< "$counts"
        done_count=${done_count:-0}
        inflight_count=${inflight_count:-0}
        pending_count=${pending_count:-0}
        if [ "$done_count" -ge 1 ] && [ "$inflight_count" -ge 1 ] && [ "$pending_count" -ge 1 ]; then
            return 0
        fi
        case "$state" in
            done|completed_with_errors|failed)
                die "job reached $state before a done/inflight/pending kill point was observed"
                ;;
        esac
        sleep 0.25
    done
    die 'timed out waiting for a completed chunk, an inflight chunk, and a pending chunk'
}

wait_for_expired_chunk_lease() {
    local job_id=$1
    local deadline=$((SECONDS + CHUNK_LEASE_SECONDS + 30))
    while (( SECONDS < deadline )); do
        if [ "$(db_query "SELECT COUNT(*) FROM chunks WHERE job_id='$job_id' AND status='inflight' AND julianday(lease_expires_at) <= julianday('now');" | tail -n 1)" -gt 0 ] \
            && [ "$(db_query "SELECT COUNT(*) FROM jobs WHERE id='$job_id' AND julianday(lease_expires_at) <= julianday('now');" | tail -n 1)" -gt 0 ]; then
            return 0
        fi
        sleep 1
    done
    die 'timed out waiting for the killed chunk lease to expire'
}

python3 - "$sample_file" <<'PY'
from pathlib import Path
import sys
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

output = Path(sys.argv[1])
paragraphs = []
for index in range(1, 321):
    text = (
        f"Operational review section {index} explains how the service team "
        "checks document delivery, records a clear status, and contacts the "
        "project owner when a translation needs attention."
    )
    paragraphs.append(f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>")
document = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body></w:document>"
)
content_types = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
relationships = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '</Relationships>'
)
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    archive.writestr("[Content_Types].xml", content_types)
    archive.writestr("_rels/.rels", relationships)
    archive.writestr("word/document.xml", document)
PY

printf 'Starting isolated offline compose project %s\n' "$project"
"${compose[@]}" up --build --detach
wait_for_services

upload_response=$(curl --fail --silent --show-error \
    --form "file=@$sample_file;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document" \
    "$web_url/api/documents")
document_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$upload_response")

document_deadline=$((SECONDS + 90))
document_status=
while (( SECONDS < document_deadline )); do
    document_status=$(curl --fail --silent --show-error "$web_url/api/documents/$document_id" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
    case "$document_status" in
        extracted) break ;;
        failed) die "document analysis failed for $document_id" ;;
    esac
    sleep 0.5
done
[ "$document_status" = extracted ] || die 'timed out waiting for document analysis'

job_request=$(python3 -c 'import json,sys; print(json.dumps({"document_id":sys.argv[1],"target_languages":["de"],"idempotency_key":sys.argv[2]}))' "$document_id" "$project")
job_response=$(curl --fail --silent --show-error \
    --header 'Content-Type: application/json' \
    --data "$job_request" \
    "$web_url/api/jobs")
job_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["jobs"][0]["id"])' <<< "$job_response")

chunk_count=$(db_query "SELECT COUNT(*) FROM chunks WHERE job_id='$job_id';" | tail -n 1)
[ "$chunk_count" -ge 3 ] || die "expected several chunks, got $chunk_count"
wait_for_job_state "$job_id"

before_done=$(db_query "SELECT id || '|' || seq FROM chunks WHERE job_id='$job_id' AND status='done' ORDER BY seq;")
before_attempts=$(db_query "SELECT c.id || '|' || COUNT(a.id) FROM chunks c LEFT JOIN chunk_attempts a ON a.chunk_id=c.id WHERE c.job_id='$job_id' GROUP BY c.id ORDER BY c.seq;")
before_attempt_total=$(db_query "SELECT COUNT(*) FROM chunk_attempts a JOIN chunks c ON c.id=a.chunk_id WHERE c.job_id='$job_id';" | tail -n 1)
before_translation_total=$(db_query "SELECT COUNT(*) FROM block_translations t JOIN blocks b ON b.source_hash=t.source_hash WHERE b.document_id='$document_id';" | tail -n 1)
done_before=$(db_query "SELECT COUNT(*) FROM chunks WHERE job_id='$job_id' AND status='done';" | tail -n 1)
inflight_before=$(db_query "SELECT id FROM chunks WHERE job_id='$job_id' AND status='inflight' LIMIT 1;" | tail -n 1)
[ -n "$inflight_before" ] || die 'no inflight chunk found at the kill point'

printf 'Before kill: done chunks=%s, inflight chunk=%s, attempts=%s, committed translations=%s\n' \
    "$done_before" "$inflight_before" "$before_attempt_total" "$before_translation_total"
"${compose[@]}" kill -s KILL worker
wait_for_expired_chunk_lease "$job_id"
"${compose[@]}" up --detach --no-deps worker

job_deadline=$((SECONDS + 240))
job_status=
while (( SECONDS < job_deadline )); do
    job_status=$(db_query "SELECT status FROM jobs WHERE id='$job_id';" | tail -n 1)
    case "$job_status" in
        done|completed_with_errors|failed) break ;;
    esac
    sleep 1
done
case "$job_status" in
    done|completed_with_errors) ;;
    *) die "job did not recover to a successful terminal state (status: ${job_status:-missing})" ;;
esac

after_done=$(db_query "SELECT id || '|' || seq FROM chunks WHERE job_id='$job_id' AND status='done' ORDER BY seq;")
after_attempts=$(db_query "SELECT c.id || '|' || COUNT(a.id) FROM chunks c LEFT JOIN chunk_attempts a ON a.chunk_id=c.id WHERE c.job_id='$job_id' GROUP BY c.id ORDER BY c.seq;")
after_attempt_total=$(db_query "SELECT COUNT(*) FROM chunk_attempts a JOIN chunks c ON c.id=a.chunk_id WHERE c.job_id='$job_id';" | tail -n 1)
after_translation_total=$(db_query "SELECT COUNT(*) FROM block_translations t JOIN blocks b ON b.source_hash=t.source_hash WHERE b.document_id='$document_id';" | tail -n 1)
block_total=$(db_query "SELECT COUNT(*) FROM blocks WHERE document_id='$document_id';" | tail -n 1)
distinct_source_total=$(db_query "SELECT COUNT(DISTINCT source_hash) FROM blocks WHERE document_id='$document_id';" | tail -n 1)
done_after=$(db_query "SELECT COUNT(*) FROM chunks WHERE job_id='$job_id' AND status='done';" | tail -n 1)
unfinished_after=$(db_query "SELECT COUNT(*) FROM chunks WHERE job_id='$job_id' AND status!='done';" | tail -n 1)

[ "$done_after" -ge "$done_before" ] || die 'done chunk progress moved backwards'
[ "$unfinished_after" -eq 0 ] || die "$unfinished_after chunks did not reach done"
[ "$after_translation_total" -eq "$block_total" ] || die "committed translations ($after_translation_total) do not cover all document blocks ($block_total)"
[ "$after_translation_total" -ge "$before_translation_total" ] || die 'committed translation count moved backwards'

declare -A before_attempt_count=()
while IFS='|' read -r chunk_id attempt_count; do
    [ -n "$chunk_id" ] && before_attempt_count["$chunk_id"]=$attempt_count
done <<< "$before_attempts"
while IFS='|' read -r chunk_id _seq; do
    [ -n "$chunk_id" ] || continue
    before_count=${before_attempt_count[$chunk_id]:-0}
    after_count=$(awk -F '|' -v id="$chunk_id" '$1 == id { print $2 }' <<< "$after_attempts")
    after_count=${after_count:-0}
    [ "$after_count" -eq "$before_count" ] || die "previously committed chunk $chunk_id gained attempts ($before_count -> $after_count)"
done <<< "$before_done"

unique_translation_total=$(db_query "SELECT COUNT(DISTINCT t.translation_key || ':' || t.source_hash) FROM block_translations t JOIN blocks b ON b.source_hash=t.source_hash WHERE b.document_id='$document_id';" | tail -n 1)
[ "$unique_translation_total" -eq "$distinct_source_total" ] || die 'duplicate committed translation identities were observed'
"${compose[@]}" exec -T web test -s "/data/out/$job_id/chaos-sample.docx" \
    || die 'translated output artifact is missing or empty'

printf 'After restart: job=%s, done chunks=%s/%s, committed translations=%s/%s\n' \
    "$job_status" "$done_after" "$chunk_count" "$after_translation_total" "$block_total"
printf 'Persisted attempt rows: %s before kill, %s after recovery; interrupted provider calls may have no row until a result is recorded.\n' \
    "$before_attempt_total" "$after_attempt_total"
printf 'Committed translation boundary: %s unique block translations; previously done chunks retained their attempt counts.\n' \
    "$after_translation_total"
printf 'Output artifact: /data/out/%s/chaos-sample.docx\n' "$job_id"

#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
    set -- web
fi

role=$1
shift

case "$role" in
    web)
        exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000 "$@"
        ;;
    worker)
        exec python -m app.worker "$@"
        ;;
    mcp)
        exec python -m app.mcp_server "$@"
        ;;
    *)
        exec "$role" "$@"
        ;;
esac

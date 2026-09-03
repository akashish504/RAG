#!/bin/sh
set -eu

if [ -z "${API_BEARER_TOKEN:-}" ]; then
    echo "ERROR: API_BEARER_TOKEN must be set (long-lived bearer secret)." >&2
    exit 1
fi

UVICORN_HOST="${UVICORN_HOST:-127.0.0.1}"
UVICORN_PORT="${UVICORN_PORT:-8000}"

echo "Starting Uvicorn on ${UVICORN_HOST}:${UVICORN_PORT} (behind Nginx on port 80)"
python -m uvicorn pipeline.api.main:app --host "$UVICORN_HOST" --port "$UVICORN_PORT" &

i=0
while [ "$i" -lt 30 ]; do
    if python -c "import urllib.request; urllib.request.urlopen('http://${UVICORN_HOST}:${UVICORN_PORT}/health', timeout=1)" >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 0.2
done

if [ "$i" -eq 30 ]; then
    echo "ERROR: Uvicorn did not become ready on /health" >&2
    exit 1
fi

echo "Starting Nginx (public HTTP on port 80 -> Uvicorn)"
exec nginx -g "daemon off;"

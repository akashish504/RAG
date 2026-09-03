#!/usr/bin/env bash
# One-shot deployment reset for the Dalberg MCP EC2 box.
#
#   bash scripts/reset_deployment.sh [--build] [--skip-preflight]
#
# What it does, in order:
#   1. Pre-flight: docker daemon, flock, .env, SQS_QUEUE_URL, queue reachability
#   2. WIPES THE ENTIRE CRONTAB for the current user (intentional — the
#      Airtable poller is the only cron job this host should carry)
#   3. docker compose down (api + worker + orphaned one-off containers)
#   4. Optional image rebuild (--build)
#   5. docker compose up -d api worker
#   6. CloudWatch Agent setup (memory/disk metrics — warns, never blocks)
#   7. Health checks: API /health, worker container, poller --dry-run
#   8. Registers the weekly poller cron entry (Sat 09:00, flock-guarded)
#
# All compose invocations include docker-compose.cloudwatch.yml (awslogs
# log shipping) when present; opt out with MCP_CLOUDWATCH_LOGS=0 on a box
# whose instance role lacks the CloudWatch Logs permissions.
#
# Idempotent: safe to re-run at any time; always converges to the same state.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

CRON_MARKER="# dalberg-mcp-airtable-poller"
POLLER_LOG="/var/log/airtable_poller.log"
LOCK_FILE="/tmp/airtable-poller.lock"

# CloudWatch log shipping (docker-compose.cloudwatch.yml → awslogs driver).
COMPOSE_FILES=(-f docker-compose.yml)
if [ "${MCP_CLOUDWATCH_LOGS:-1}" != "0" ] && [ -f "${REPO_ROOT}/docker-compose.cloudwatch.yml" ]; then
    COMPOSE_FILES+=(-f docker-compose.cloudwatch.yml)
fi

BUILD=0
SKIP_PREFLIGHT=0
for arg in "$@"; do
    case "${arg}" in
        --build) BUILD=1 ;;
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        *) echo "Unknown flag: ${arg}" >&2; echo "Usage: $0 [--build] [--skip-preflight]" >&2; exit 2 ;;
    esac
done

say()  { printf '\n==> %s\n' "$*"; }
pass() { printf '    [PASS] %s\n' "$*"; }
warn() { printf '    [WARN] %s\n' "$*"; }
fail() { printf '    [FAIL] %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- pre-flight
if [ "${SKIP_PREFLIGHT}" -eq 0 ]; then
    say "Pre-flight checks"

    command -v docker >/dev/null 2>&1 || fail "docker not found on PATH"
    DOCKER_BIN="$(command -v docker)"
    docker info >/dev/null 2>&1 || fail "docker daemon not reachable"
    pass "docker daemon reachable (${DOCKER_BIN})"

    command -v flock >/dev/null 2>&1 || fail "flock not found (needed by the cron entry)"
    pass "flock available"

    [ -f "${REPO_ROOT}/.env" ] || fail ".env not found in ${REPO_ROOT}"
    QUEUE_URL="$(grep -E '^SQS_QUEUE_URL=' "${REPO_ROOT}/.env" | tail -1 | cut -d= -f2- || true)"
    [ -n "${QUEUE_URL}" ] || fail "SQS_QUEUE_URL is empty/missing in .env"
    pass ".env present with SQS_QUEUE_URL"

    # Queue reachability. Exit 3 = queue OK but no DLQ — a WARN by design:
    # the S3 job ledger (_pipeline_state/jobs/) is the DLQ substitute.
    set +e
    docker compose "${COMPOSE_FILES[@]}" run --rm pipeline python scripts/check_sqs.py
    CHECK_RC=$?
    set -e
    if [ "${CHECK_RC}" -eq 0 ]; then
        pass "SQS queue reachable"
    elif [ "${CHECK_RC}" -eq 3 ]; then
        warn "SQS queue reachable but has no DLQ (expected — S3 job ledger is the DLQ substitute)"
    else
        fail "check_sqs.py exited ${CHECK_RC} — fix queue config before deploying"
    fi
else
    say "Pre-flight SKIPPED (--skip-preflight)"
    command -v docker >/dev/null 2>&1 || fail "docker not found on PATH"
    DOCKER_BIN="$(command -v docker)"
fi

# ------------------------------------------------- remove cron before teardown
# Wipe FIRST so no poller fires mid-reset. Full-crontab wipe is intentional
# and was explicitly confirmed: the poller is this host's only cron job.
say "Wiping crontab (full wipe — intentional)"
crontab -r 2>/dev/null || true
pass "crontab empty"

# ------------------------------------------------------------------ teardown
say "Stopping containers (api, worker, orphans) — project dalberg-mcp"
docker compose "${COMPOSE_FILES[@]}" down --remove-orphans
pass "all project containers stopped"

# ------------------------------------------------------------ optional build
if [ "${BUILD}" -eq 1 ]; then
    say "Rebuilding images (api + pipeline; worker shares the pipeline image)"
    docker compose "${COMPOSE_FILES[@]}" build api pipeline
    pass "images rebuilt"
fi

# ------------------------------------------------------------------- startup
say "Starting api + worker"
docker compose "${COMPOSE_FILES[@]}" up -d api worker
pass "compose up dispatched"

# ---------------------------------------------------------- monitoring agent
# warn, never fail: a missing IAM policy must not block a deploy — the
# mem/disk alarms just stay empty until the role is fixed.
say "CloudWatch Agent (memory/disk metrics)"
if bash "${SCRIPT_DIR}/setup_monitoring.sh"; then
    pass "CloudWatch Agent running"
else
    warn "CloudWatch Agent setup failed — mem/disk alarms will have no data (check instance IAM role)"
fi

# -------------------------------------------------------------- health checks
say "Health check: API /health"
API_HOST_PORT="$(grep -E '^API_HOST_PORT=' "${REPO_ROOT}/.env" | tail -1 | cut -d= -f2- || true)"
API_HOST_PORT="${API_HOST_PORT:-80}"
API_OK=0
for _ in $(seq 1 30); do
    if curl -fsS "http://localhost:${API_HOST_PORT}/health" >/dev/null 2>&1; then
        API_OK=1
        break
    fi
    sleep 2
done
[ "${API_OK}" -eq 1 ] && pass "API healthy on :${API_HOST_PORT}" || fail "API /health not responding after 60s (docker compose logs api)"

say "Health check: worker container"
WORKER_OK=0
for _ in $(seq 1 30); do
    if docker compose "${COMPOSE_FILES[@]}" ps --status running worker 2>/dev/null | grep -q worker; then
        WORKER_OK=1
        break
    fi
    sleep 2
done
[ "${WORKER_OK}" -eq 1 ] || fail "worker container not running after 60s (docker compose logs worker)"
if docker compose "${COMPOSE_FILES[@]}" logs --tail 40 worker 2>/dev/null | grep -q "Airtable Ingestion Worker"; then
    pass "worker running (startup banner seen)"
else
    warn "worker running but startup banner not seen yet (docker compose logs worker)"
fi

say "Health check: poller dry run (Airtable creds + cursor read, no enqueue)"
docker compose "${COMPOSE_FILES[@]}" run --rm pipeline python scripts/run_poller.py --dry-run \
    || fail "poller --dry-run failed (check AIRTABLE_PAT_TOKEN / S3 access)"
pass "poller dry run OK"

# ------------------------------------------------------------- register cron
say "Registering poller cron (weekly: Saturday 09:00 server time)"
touch "${POLLER_LOG}" 2>/dev/null || warn "could not touch ${POLLER_LOG} (cron will try to create it)"
CRON_LINE="0 9 * * 6 cd ${REPO_ROOT} && flock -n ${LOCK_FILE} ${DOCKER_BIN} compose ${COMPOSE_FILES[*]} run --rm pipeline python scripts/run_poller.py >> ${POLLER_LOG} 2>&1 ${CRON_MARKER}"
printf '%s\n' "${CRON_LINE}" | crontab -
pass "cron registered"

# -------------------------------------------------------------------- summary
cat <<EOF

============================================================
  DEPLOYMENT RESET COMPLETE
============================================================
  Services   : api (:${API_HOST_PORT}), worker (SQS consumer)
  Cron       : ${CRON_LINE}
  Poller log : ${POLLER_LOG}
  Failures   : docker compose run --rm pipeline python scripts/list_ingestion_failures.py

  Schedule   : poller runs WEEKLY — Saturday 09:00 in the
               server's local time (check with: date; EC2 boxes
               default to UTC). To change it, edit CRON_LINE in
               scripts/reset_deployment.sh and re-run, or run a
               one-off poll now with:
               docker compose run --rm pipeline python scripts/run_poller.py
EOF

#!/usr/bin/env bash
# Spawn extra on-demand runner workers while there is a job backlog, then exit.
#
# The always-on runner (start_runner.sh) drives one job at a time, and its
# EXECUTE stage can hold it for hours while a DFT job runs on the cluster --
# so other users' queued jobs wait even though the cluster has free nodes.
# This script watches the queue and keeps up to MAX one-shot workers alive
# (`runner.runner --once`: claim one job, drive it, exit). Safe by design:
# job claiming is FOR UPDATE SKIP LOCKED and per-session serialized, so
# workers never collide with the main runner or each other, and a killed
# worker's job is re-queued by the reaper once its lease expires.
#
# Manual:  bash scripts/ris/scale_runners.sh [N]   # supervise until the queue
#                                                  # drains, then exit (N<=3:
#                                                  # login-node RAM cap)
# Automatic (recommended): install the cron entry once --
#
#   bash scripts/ris/scale_runners.sh --install-cron
#
# Cron then fires every minute: when the queue is idle the run exits in ~2s
# and logs nothing; when a backlog appears it becomes the supervisor until
# the queue drains. A flock guard keeps it single-instance, so overlapping
# cron fires (or a manual run) are no-ops. Tune the worker cap by setting
# TWAIN_MAX_EXTRA_RUNNERS in the deploy dir's .env.
set -euo pipefail

if [ "${1:-}" = "--install-cron" ]; then
  RIS_DIR="${RIS_DIR:-${CODE_DIR:-/storage2/fs1/mdan/Active/common/projects/twain/TWAIN}}"
  line="* * * * * bash $RIS_DIR/scripts/ris/scale_runners.sh --cron >> $RIS_DIR/scale-runners.log 2>&1"
  # `|| true`: grep exits 1 on an empty crontab, which set -e would turn into
  # an abort before the new line is appended.
  (crontab -l 2>/dev/null | grep -vF "scale_runners.sh" || true; echo "$line") | crontab -
  echo "Installed cron entry:"
  crontab -l | grep scale_runners.sh
  exit 0
fi

CRON_MODE=0
if [ "${1:-}" = "--cron" ]; then CRON_MODE=1; shift; fi

. "$(dirname "$0")/runner_env.sh"   # cd to deploy dir, PATH, Lmod, .env

MAX_EXTRA="${1:-${TWAIN_MAX_EXTRA_RUNNERS:-2}}"
POLL_SECONDS=15

# Single supervisor at a time: a second invocation (the next cron fire, or a
# manual run alongside cron) exits immediately instead of doubling workers.
exec 9>".scale-runners.lock"
flock -n 9 || exit 0

log() { echo "[scale $(date '+%F %T')] $*"; }

# Each worker gets its own log. Previously a worker inherited the supervisor's
# stdout, so under cron its whole run landed in scale-runners.log mixed with
# scaling chatter -- and nothing told the reader that, because the run-error
# block names $TWAIN_RUN_LOG, which defaulted to the always-on runner's file.
WORKER_LOG_DIR="$RIS_DIR/logs/workers"
spawned=0

# Bound the file count. Workers are one-shot (`--once`: claim one job, run it,
# exit) and can be spawned every few seconds under sustained backlog, and
# nothing in scripts/ris rotates or prunes any log -- so without this the
# directory grows without limit on shared storage.
KEEP_WORKER_LOGS="${TWAIN_KEEP_WORKER_LOGS:-40}"
prune_worker_logs() {
  ls -1t "$WORKER_LOG_DIR"/worker-*.log 2>/dev/null \
    | tail -n "+$((KEEP_WORKER_LOGS + 1))" | while IFS= read -r old; do
      rm -f "$old"
    done
}

# Claimable backlog only: mirror claim_job()'s per-session guard, else a
# queued-but-blocked job (its session already has a running job) would make
# us churn workers that claim nothing and exit. Direct env python (no `pixi
# run`) keeps the every-minute idle check cheap; prints "unknown" when the
# DB is unreachable so the caller can defer instead of crashing.
claimable_jobs() {
  ".pixi/envs/default/bin/python" - <<'PY'
import os
try:
    import psycopg2
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "twaindb"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
        connect_timeout=8,
    )
    with conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM jobs
            WHERE status = 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM jobs active
                  WHERE active.session_id = jobs.session_id
                    AND active.status IN ('claimed', 'running')
              );
        """)
        print(cur.fetchone()[0])
    conn.close()
except Exception:  # noqa: BLE001 - unreachable DB -> caller defers
    print("unknown")
PY
}

if [ "$CRON_MODE" -eq 0 ]; then
  log "watching the queue on $(hostname) (max $MAX_EXTRA extra workers; Ctrl-C to stop)"
fi

while true; do
  workers="$(jobs -pr | wc -l | tr -d ' ')"
  backlog="$(claimable_jobs)"
  case "$backlog" in
    ''|*[!0-9]*)  # DB unreachable/garbled: babysit live workers, else stand down
      if [ "$workers" -eq 0 ]; then exit 0; fi
      sleep "$POLL_SECONDS"
      continue
      ;;
  esac
  if [ "$backlog" -gt 0 ] && [ "$workers" -lt "$MAX_EXTRA" ]; then
    mkdir -p "$WORKER_LOG_DIR"
    prune_worker_logs
    # Timestamp + counter, not $!: the PID is only known after the spawn, and the
    # worker needs to be told its own log path before it starts.
    spawned=$((spawned + 1))
    worker_log="$WORKER_LOG_DIR/worker-$(date '+%Y%m%d-%H%M%S')-$spawned.log"
    # Same launcher as start_runner.sh (pixi run provides activation env like
    # DFTB_PREFIX, which local-execution jobs on the login node rely on).
    #
    # 9>&-: do NOT let a worker inherit the flock fd. A worker that outlives a
    # killed supervisor would otherwise hold .scale-runners.lock forever, so every
    # later cron fire exits silently at the flock -- the same trap auto_update.sh
    # documents for its tmux server.
    TWAIN_RUN_LOG="$worker_log" \
      pixi run python -m runner.runner --once >>"$worker_log" 2>&1 9>&- &
    log "spawned worker $! (backlog: $backlog, workers: $((workers + 1)))" \
        "-> $worker_log"
    sleep 3  # let it claim its job before we recount the backlog
    continue
  fi
  if [ "$backlog" -eq 0 ] && [ "$workers" -eq 0 ]; then
    if [ "$CRON_MODE" -eq 0 ]; then
      log "queue drained and all workers finished; exiting"
    fi
    exit 0
  fi
  sleep "$POLL_SECONDS"
done

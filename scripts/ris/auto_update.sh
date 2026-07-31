#!/usr/bin/env bash
# Auto-update the RIS backend from GitHub master (run by cron ON the login node).
#
# Every run: fetch origin/master; if it moved AND the runner is idle, reset the
# deploy dir to it, refresh the pixi env, and restart the runner tmux session.
# If the runner is mid-job (a claimed/running row in the jobs table, or a Slurm
# job in the queue) the update is deferred to the next cron cycle -- a run is
# never interrupted. The repo is public, so fetching needs no credentials; the
# cluster-only files (.env, runner-ris.log) are untracked and survive resets.
#
# Install (one time, on the login node):
#   bash scripts/ris/auto_update.sh --install-cron   # polls every 10 minutes
#
# Logs to auto-update.log next to the deploy dir's runner-ris.log.
set -euo pipefail

RIS_DIR="${RIS_DIR:-/storage2/fs1/mdan/Active/dtrc2026-workshop/twain-backend}"
REPO_URL="${REPO_URL:-https://github.com/washu-dev/TWAIN.git}"
BRANCH="${BRANCH:-master}"
export PATH="$HOME/.pixi/bin:$PATH"

log() { echo "[auto-update $(date '+%F %T')] $*"; }

if [ "${1:-}" = "--install-cron" ]; then
  line="*/10 * * * * bash $RIS_DIR/scripts/ris/auto_update.sh >> $RIS_DIR/auto-update.log 2>&1"
  # `|| true`: grep exits 1 on an empty crontab, which set -e would turn into
  # an abort before the new line is appended.
  (crontab -l 2>/dev/null | grep -vF "auto_update.sh" || true; echo "$line") | crontab -
  echo "Installed cron entry:"
  crontab -l | grep auto_update.sh
  exit 0
fi

cd "$RIS_DIR"

# Single instance: overlapping cron runs (e.g. a slow pixi install) skip out.
exec 9>".auto-update.lock"
flock -n 9 || exit 0

# One-time bootstrap: the dir was originally rsync-deployed (no .git). Turning
# it into a clone in place preserves the untracked .env and logs.
if [ ! -d .git ]; then
  log "bootstrapping git clone in $RIS_DIR"
  git init -q
  git remote add origin "$REPO_URL"
  git fetch -q --depth 50 origin "$BRANCH"
  git checkout -q -f -B "$BRANCH" FETCH_HEAD
fi

git fetch -q origin "$BRANCH"
local_rev="$(git rev-parse HEAD)"
remote_rev="$(git rev-parse FETCH_HEAD)"
[ "$local_rev" = "$remote_rev" ] && exit 0
log "master moved: $(git rev-parse --short HEAD) -> $(git rev-parse --short FETCH_HEAD)"

# Defer while the runner is working. The jobs table is authoritative (any
# claimed/running row means a run is in flight, whatever the stage); the squeue
# check is best-effort extra safety for a Slurm job the DB somehow missed.
if command -v squeue >/dev/null 2>&1 && [ -n "$(squeue -u "$(whoami)" -h 2>/dev/null)" ]; then
  log "Slurm jobs in the queue -- deferring update"
  exit 0
fi
busy="$(
  set -a; . ./.env; set +a
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
        cur.execute("select count(*) from jobs where status in ('claimed', 'running')")
        print(cur.fetchone()[0])
except Exception:  # noqa: BLE001 - unreachable DB -> report unknown, caller defers
    print("unknown")
PY
)"
if [ "$busy" != "0" ]; then
  log "runner busy or DB unreachable (active jobs: $busy) -- deferring update"
  exit 0
fi

git reset -q --hard "$remote_rev"
log "checked out $(git rev-parse --short HEAD); refreshing pixi env"
pixi install >/dev/null

log "restarting runner tmux session"
tmux kill-session -t twain-runner 2>/dev/null || true
sleep 1
# 9>&-: do NOT let the tmux server inherit the flock fd. Without it, the
# daemonized tmux (which outlives this script) held .auto-update.lock forever,
# so every later cron tick exited silently at the flock and the box never
# updated again after its first successful auto-update.
tmux new-session -d -s twain-runner \
  "bash $RIS_DIR/scripts/ris/start_runner.sh 2>&1 | tee -a $RIS_DIR/runner-ris.log" 9>&-
sleep 5
if pgrep -f "runner.runner" >/dev/null; then
  log "runner restarted on $(git rev-parse --short HEAD)"
else
  log "ERROR: runner did not come back after restart -- check runner-ris.log"
  exit 1
fi

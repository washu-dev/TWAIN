#!/usr/bin/env bash
# Auto-update the RIS backend from GitHub master (run by cron ON the login node).
#
# Every run: fetch origin/master; if it moved AND the runner is idle, reset the
# deploy dir to it, refresh the pixi env, and restart the runner tmux session.
# If the runner is mid-job (a claimed/running row in the jobs table, or a Slurm
# job in the queue) the update is deferred to the next cron cycle -- a run is
# never interrupted. The cluster-only files (.env, runner-ris.log) are untracked
# and survive resets.
#
# The repo is PRIVATE (since Aug 2026), so fetching needs a credential: set
# TWAIN_DEPLOY_KEY in the deploy dir's .env to a read-only GitHub deploy key
# (chmod 600, outside the shared tree, e.g. ~/.ssh/twain_deploy). Without one the
# fetch fails -- it did, silently, 7,186 times while production ran Aug 6 code --
# so a failure now writes auto-update.status and one loud line per run.
#
# Install (one time, on the login node):
#   bash scripts/ris/auto_update.sh --install-cron   # polls every 10 minutes
#
# Logs to auto-update.log next to the deploy dir's runner-ris.log.
set -euo pipefail

RIS_DIR="${RIS_DIR:-/storage2/fs1/mdan/Active/dtrc2026-workshop/twain-backend}"
# Per-deploy overrides (TWAIN_DEPLOY_KEY, REPO_URL, ...) live in the untracked .env.
if [ -f "$RIS_DIR/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$RIS_DIR/.env"; set +a
fi
if [ -n "${TWAIN_DEPLOY_KEY:-}" ]; then
  REPO_URL="${REPO_URL:-git@github.com:washu-dev/TWAIN.git}"
  export GIT_SSH_COMMAND="ssh -i $TWAIN_DEPLOY_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
fi
REPO_URL="${REPO_URL:-https://github.com/washu-dev/TWAIN.git}"
STATUS_FILE="$RIS_DIR/auto-update.status"
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

# Follow REPO_URL (e.g. https -> ssh once a deploy key is configured).
git remote set-url origin "$REPO_URL"
if ! fetch_err="$(git fetch -q origin "$BRANCH" 2>&1)"; then
  # `|| true`: no status file yet on the first failure, and pipefail + set -e
  # would otherwise exit here -- before the loud line this block exists for.
  since="$(sed -n 's/^failing since //p' "$STATUS_FILE" 2>/dev/null | head -1 || true)"
  { echo "failing since ${since:-$(date '+%F %T')}"; echo "last error: $fetch_err"; } > "$STATUS_FILE"
  log "ERROR: cannot fetch $REPO_URL (${fetch_err%%$'\n'*}) -- the runner stays on" \
      "$(git rev-parse --short HEAD). Private repo: set TWAIN_DEPLOY_KEY in $RIS_DIR/.env" \
      "(read-only deploy key) -- see runner/README.md 'Keeping the RIS runner current'."
  exit 1
fi
rm -f "$STATUS_FILE"
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

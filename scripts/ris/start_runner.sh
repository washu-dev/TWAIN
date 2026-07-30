#!/usr/bin/env bash
# Run the TWAIN runner ON the RIS login node (inside tmux or nohup).
#
# The runner is light -- it polls the jobs table, calls the LLM, and submits
# the actual computation to compute nodes via sbatch -- so it fits the login
# node's 6 GB/user cap. It restarts automatically if it crashes.
#
#   tmux new -s twain-runner
#   bash scripts/ris/start_runner.sh
set -euo pipefail

RIS_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RIS_DIR"
export PATH="$HOME/.pixi/bin:$PATH"
# Unbuffered so `tail -f runner-ris.log` shows activity in real time.
export PYTHONUNBUFFERED=1

# sbatch/squeue for the runner's own submissions (jobs load modules themselves).
# `module` is a shell function that only exists after Lmod init, which login
# shells get from /etc/profile -- but a tmux session started from cron (the
# auto-updater) or a non-interactive ssh gets neither. Initialize it
# explicitly, then fall back to the Slurm bin dir, and refuse to start a
# runner that cannot submit jobs (a silent PATH failure here surfaces as
# "[Errno 2] No such file or directory: 'sbatch'" on every user's run).
if ! command -v module >/dev/null 2>&1; then
  set +u; . /etc/profile >/dev/null 2>&1 || true; set -u
fi
module load ris slurm >/dev/null 2>&1 || true
if ! command -v sbatch >/dev/null 2>&1 && [ -x /cm/local/apps/slurm/current/bin/sbatch ]; then
  export PATH="/cm/local/apps/slurm/current/bin:$PATH"
fi
if ! command -v sbatch >/dev/null 2>&1; then
  echo "FATAL: sbatch not on PATH after module load + fallback; not starting" >&2
  exit 1
fi

# Secrets + DB config (deploy.sh installed .env from your .env.ris).
set -a; . ./.env; set +a

# The backend runs ON the cluster:
#  - TWAIN_SLURM_HOST=  (empty) -> sbatch + staging happen locally, no SSH
#  - Slurm is the default compute target; the UI selector still applies per run
#  - execution is on (the plan-approval gate still applies)
export TWAIN_SLURM_HOST=
export TWAIN_EXECUTE_SLURM="${TWAIN_EXECUTE_SLURM:-1}"
export TWAIN_EXECUTE_LOCALLY="${TWAIN_EXECUTE_LOCALLY:-1}"

echo "==> TWAIN runner starting on $(hostname) (DB: ${DB_HOST:-unset})"
while true; do
  pixi run python -m runner.runner || true
  echo "==> runner exited; restarting in 10s (Ctrl-C to stop)"
  sleep 10
done

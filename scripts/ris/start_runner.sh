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
module load ris slurm 2>/dev/null || true

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

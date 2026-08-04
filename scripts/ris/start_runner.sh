#!/usr/bin/env bash
# Run the TWAIN runner ON the RIS login node (inside tmux or nohup).
#
# The runner is light -- it polls the jobs table, calls the LLM, and submits
# the actual computation to compute nodes via sbatch -- so it fits the login
# node's 6 GB/user cap. It restarts automatically if it crashes.
#
#   tmux new -s twain-runner
#   bash scripts/ris/start_runner.sh
#
# When a backlog builds behind a long-running job, add temporary workers with
# scripts/ris/scale_runners.sh (they exit on their own once the queue drains).
set -euo pipefail

. "$(dirname "$0")/runner_env.sh"

echo "==> TWAIN runner starting on $(hostname) (DB: ${DB_HOST:-unset})"
while true; do
  pixi run python -m runner.runner || true
  echo "==> runner exited; restarting in 10s (Ctrl-C to stop)"
  sleep 10
done

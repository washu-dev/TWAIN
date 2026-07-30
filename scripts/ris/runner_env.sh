# Shared environment setup for RIS runner processes. SOURCE this, don't run it:
# both start_runner.sh (the always-on runner) and scale_runners.sh (on-demand
# extra workers) need the identical setup, and a copy that drifts is how the
# "sbatch not on PATH" failure snuck back in via cron-started tmux sessions.

RIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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

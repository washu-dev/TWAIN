#!/usr/bin/env bash
# One-time setup ON the RIS login node: install pixi (user-local), install the
# TWAIN environment, and verify the runner can reach its two dependencies
# (the shared Postgres on AWS RDS, and the WashU LLM gateway).
#
# Invoked by scripts/ris/deploy.sh; can also be run by hand:
#   bash scripts/ris/setup.sh /path/to/twain-backend
set -euo pipefail

RIS_DIR="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$RIS_DIR"

echo "==> Host: $(hostname) ($(uname -m))"

# -- pixi (static binary, installs to ~/.pixi/bin) ---------------------------
export PATH="$HOME/.pixi/bin:$PATH"
if ! command -v pixi >/dev/null 2>&1; then
  echo "==> Installing pixi"
  curl -fsSL https://pixi.sh/install.sh | bash
  export PATH="$HOME/.pixi/bin:$PATH"
fi
echo "==> pixi $(pixi --version)"

# -- TWAIN environment --------------------------------------------------------
# The runner only needs the default env (the heavy calculators run inside
# Slurm jobs using the pre-provisioned twain-envs, not in this env).
echo "==> pixi install (default env)"
pixi install

# -- connectivity checks -------------------------------------------------------
fail=0

DB_HOST="$(sed -n 's/^DB_HOST=//p' .env | tail -1)"
DB_PORT="$(sed -n 's/^DB_PORT=//p' .env | tail -1)"
if timeout 8 bash -c "exec 3<>/dev/tcp/${DB_HOST}/${DB_PORT:-5432}" 2>/dev/null; then
  echo "==> OK: Postgres reachable at $DB_HOST:${DB_PORT:-5432}"
else
  echo "==> FAIL: cannot reach Postgres at $DB_HOST:${DB_PORT:-5432}" >&2
  fail=1
fi

LLM_HOST="aiapi.wustl.edu"
if timeout 8 bash -c "exec 3<>/dev/tcp/${LLM_HOST}/443" 2>/dev/null; then
  echo "==> OK: LLM gateway reachable at $LLM_HOST:443"
else
  echo "==> FAIL: cannot reach the LLM gateway at $LLM_HOST:443" >&2
  fail=1
fi

if command -v sbatch >/dev/null 2>&1 || module load ris slurm 2>/dev/null && command -v sbatch >/dev/null 2>&1; then
  echo "==> OK: sbatch available ($(command -v sbatch))"
else
  echo "==> FAIL: sbatch not found even after 'module load ris slurm'" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "Setup finished with connectivity FAILURES (see above)." >&2
  exit 1
fi
echo "==> Setup complete."

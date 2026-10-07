#!/usr/bin/env bash
# Deploy the TWAIN backend (runner) to the RIS Compute2 cluster.
#
# Run this FROM YOUR WORKSTATION (on the WUSTL VPN). It rsyncs the repo to
# cluster storage, pushes your .env.ris as the remote .env, and runs the
# remote setup (pixi install + connectivity checks).
#
#   RIS_USER=junbo.y scripts/ris/deploy.sh
#
# Overridable:
#   RIS_USER   cluster username (required, or set in your ~/.ssh/config)
#   RIS_HOST   login node          (default c2-login-001.ris.wustl.edu)
#   RIS_DIR    remote install dir  (default <team storage>/twain-backend)
set -euo pipefail

RIS_HOST="${RIS_HOST:-c2-login-001.ris.wustl.edu}"
RIS_DIR="${RIS_DIR:-${CODE_DIR:-/storage2/fs1/mdan/Active/common/projects/twain/TWAIN}}"
TARGET="${RIS_USER:+$RIS_USER@}$RIS_HOST"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f .env.ris ]; then
  echo "ERROR: .env.ris not found at the repo root." >&2
  echo "Copy scripts/ris/env.ris.example to .env.ris and fill in the secrets." >&2
  exit 1
fi

echo "==> Deploying to $TARGET:$RIS_DIR"
ssh "$TARGET" "mkdir -p '$RIS_DIR'"

# The runner needs the pipeline + configs, not the web app or local caches.
rsync -az --delete \
  --exclude '.git' --exclude '.pixi' --exclude '__pycache__' \
  --exclude '*.pyc' --exclude '.pytest_cache' \
  --exclude 'node_modules' --exclude 'app' --exclude 'terraform' \
  --exclude 'logs' --exclude '.env' --exclude '.env.ris' \
  ./ "$TARGET:$RIS_DIR/"

# .env.ris becomes the remote .env (chmod 600: it holds DB + LLM secrets).
rsync -az .env.ris "$TARGET:$RIS_DIR/.env"
ssh "$TARGET" "chmod 600 '$RIS_DIR/.env'"

echo "==> Running remote setup (pixi install may take a while on first run)"
ssh "$TARGET" "bash '$RIS_DIR/scripts/ris/setup.sh' '$RIS_DIR'"

cat <<EOF

Deployed. Start the runner on the login node with:

  ssh $TARGET
  tmux new -s twain-runner
  bash $RIS_DIR/scripts/ris/start_runner.sh

(Detach from tmux with Ctrl-B then D; reattach with 'tmux attach -t twain-runner'.)
EOF

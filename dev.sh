#!/usr/bin/env bash
#
# One-command local dev for the TWAIN web UI.
#
#   ./dev.sh                 # DB + API + runner + web app
#   ./dev.sh --no-runner     # UI only (chat won't progress past INTAKE)
#   ./dev.sh --no-app        # backend only (API + runner)
#   ./dev.sh --help
#
# First run bootstraps everything (creates the DB, applies migrations, builds the
# API venv, installs app + pixi deps). Ctrl-C stops all processes together.
#
# Config via env (sensible local defaults):
#   DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD API_PORT AUTH_DISABLED
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-twaindb}"
DB_USER="${DB_USER:-$(whoami)}"
DB_PASSWORD="${DB_PASSWORD:-}"
API_PORT="${API_PORT:-8000}"
AUTH_DISABLED="${AUTH_DISABLED:-true}"
export DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD AUTH_DISABLED

RUN_RUNNER=1
RUN_APP=1
for arg in "$@"; do
  case "$arg" in
    --no-runner) RUN_RUNNER=0 ;;
    --no-app) RUN_APP=0 ;;
    -h|--help) sed -n '3,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

API_VENV="$REPO/api/.venv-api"

info() { printf '\033[1;36m▶ %s\033[0m\n' "$1"; }

require() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 1; }; }
require psql
require createdb
require python3

# ── 1. Database: create + migrate (idempotent) ───────────────────────────────
if ! psql -h "$DB_HOST" -p "$DB_PORT" -lqt | cut -d'|' -f1 | tr -d ' ' | grep -qx "$DB_NAME"; then
  info "Creating database '$DB_NAME'"
  createdb -h "$DB_HOST" -p "$DB_PORT" "$DB_NAME"
fi
info "Applying migrations to '$DB_NAME'"
for migration in api/migrations/*.sql; do
  psql -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -v ON_ERROR_STOP=1 -q -f "$migration"
done

# ── 2. API venv ───────────────────────────────────────────────────────────────
if [ ! -x "$API_VENV/bin/python" ]; then
  info "Creating API venv + installing deps (first run only)"
  python3 -m venv "$API_VENV"
  "$API_VENV/bin/pip" install -q --upgrade pip
  "$API_VENV/bin/pip" install -q -r api/requirements.txt
fi

# ── 3. App deps ───────────────────────────────────────────────────────────────
if [ "$RUN_APP" = 1 ] && [ ! -d app/node_modules ]; then
  require npm
  info "Installing app dependencies (first run only)"
  (cd app && npm install)
fi

# ── 4. pixi env for the runner ────────────────────────────────────────────────
if [ "$RUN_RUNNER" = 1 ]; then
  require pixi
  info "Ensuring pixi environment for the runner"
  pixi install
fi

# ── stop everything together on Ctrl-C / exit ────────────────────────────────
trap 'echo; info "Shutting down…"; kill 0 2>/dev/null || true' EXIT INT TERM

# ── 5. API ────────────────────────────────────────────────────────────────────
info "API        → http://localhost:$API_PORT  (auth ${AUTH_DISABLED:+disabled})"
( cd api && exec "$API_VENV/bin/python" -m uvicorn main:app --host 127.0.0.1 --port "$API_PORT" ) &

# ── 6. Runner ─────────────────────────────────────────────────────────────────
if [ "$RUN_RUNNER" = 1 ]; then
  info "Runner     → pixi run python -m runner.runner  (needs WashU network for the LLM)"
  ( exec pixi run python -m runner.runner ) &
fi

# ── 7. Web app ────────────────────────────────────────────────────────────────
if [ "$RUN_APP" = 1 ]; then
  info "Web app    → http://localhost:8081"
  ( cd app && EXPO_PUBLIC_API_BASE_URL="http://localhost:$API_PORT" exec npm run web ) &
fi

wait

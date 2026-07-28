#!/usr/bin/env bash
#
# One-command local dev for the TWAIN web UI.
#
#   ./dev.sh                 # DB + API + runner + web app
#   ./dev.sh --no-runner     # UI only (chat won't progress past INTAKE)
#   ./dev.sh --no-app        # backend only (API + runner)
#   ./dev.sh --no-execute    # plan-only runner (EXECUTE stage skipped)
#   ./dev.sh --help
#
# First run bootstraps everything (starts/creates the Postgres container when no
# local Postgres is available, applies migrations, builds the API venv, installs
# app + pixi deps). Ctrl-C stops all processes together.
#
# Postgres: uses a local server when `psql` can reach one; otherwise runs the
# `twain-pg` Docker container (starting Colima on macOS if needed).
#
# Config via env (sensible local defaults):
#   DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD API_PORT AUTH_DISABLED
#   PG_CONTAINER TWAIN_EXECUTE_LOCALLY TWAIN_DB_FROM_ENV
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-twaindb}"
# Use the DB_* env vars below for the API's connection, not AWS Secrets Manager
# (which has no credentials locally). Without this the API 500s on every
# DB-backed request. See api/database.py:_load_db_config.
TWAIN_DB_FROM_ENV="${TWAIN_DB_FROM_ENV:-true}"
API_PORT="${API_PORT:-8000}"
AUTH_DISABLED="${AUTH_DISABLED:-true}"
PG_CONTAINER="${PG_CONTAINER:-twain-pg}"
# Local dev executes the generated calculation for real by default (the
# plan-approval gate in the UI still applies). --no-execute or
# TWAIN_EXECUTE_LOCALLY=0 keeps the pipeline planning-only.
TWAIN_EXECUTE_LOCALLY="${TWAIN_EXECUTE_LOCALLY:-1}"
# The API resolves DB credentials from AWS Secrets Manager by default (the
# cloud path); local dev has no AWS credentials, so read the DB_* env vars
# exported below instead (api/database.py's offline escape hatch).
TWAIN_DB_FROM_ENV="${TWAIN_DB_FROM_ENV:-true}"

RUN_RUNNER=1
RUN_APP=1
for arg in "$@"; do
  case "$arg" in
    --no-runner) RUN_RUNNER=0 ;;
    --no-app) RUN_APP=0 ;;
    --no-execute) TWAIN_EXECUTE_LOCALLY=0 ;;
    -h|--help) sed -n '3,21p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

API_VENV="$REPO/api/.venv-api"

info() { printf '\033[1;36m▶ %s\033[0m\n' "$1"; }

require() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 1; }; }
require python3

# ── 1. Database: local Postgres, or the twain-pg Docker container ─────────────
# Two ways to run psql commands, so the script works both with a native install
# and with Postgres inside Docker (no psql client on the host needed).
USE_DOCKER_PG=0
# -w: never prompt for a password. Without it, when the twain-pg container is
# already up (Colima running), this probe hits the container's Postgres, which
# asks for a password interactively -- blocking the script on a prompt that can
# only fail. With -w the probe fails silently and we take the Docker path.
if command -v psql >/dev/null 2>&1 \
   && PGCONNECT_TIMEOUT=3 PGPASSWORD="${DB_PASSWORD:-}" \
      psql -w -h "$DB_HOST" -p "$DB_PORT" -U "${DB_USER:-$(whoami)}" -lqt >/dev/null 2>&1; then
  DB_USER="${DB_USER:-$(whoami)}"
  DB_PASSWORD="${DB_PASSWORD:-}"
else
  require docker
  USE_DOCKER_PG=1
  DB_USER="${DB_USER:-postgres}"
  DB_PASSWORD="${DB_PASSWORD:-postgres}"

  if ! docker info >/dev/null 2>&1; then
    if command -v colima >/dev/null 2>&1; then
      info "Starting Colima (Docker daemon)"
      colima start
    else
      echo "error: no Docker daemon reachable (install Docker Desktop or Colima)" >&2
      exit 1
    fi
  fi

  if ! docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    info "Creating Postgres container '$PG_CONTAINER' (postgres:16 on :$DB_PORT)"
    docker run -d --name "$PG_CONTAINER" -p "$DB_PORT:5432" \
      -e POSTGRES_PASSWORD="$DB_PASSWORD" -e POSTGRES_DB="$DB_NAME" postgres:16 >/dev/null
  elif ! docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    info "Starting Postgres container '$PG_CONTAINER'"
    docker start "$PG_CONTAINER" >/dev/null
  fi

  info "Waiting for Postgres to accept connections"
  for _ in $(seq 1 30); do
    docker exec "$PG_CONTAINER" pg_isready -U "$DB_USER" >/dev/null 2>&1 && break
    sleep 1
  done
fi
export DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD AUTH_DISABLED TWAIN_EXECUTE_LOCALLY TWAIN_DB_FROM_ENV

# psql against the dev DB, transparently local or via the container.
run_psql() {
  if [ "$USE_DOCKER_PG" = 1 ]; then
    docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" "$@"
  else
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$@"
  fi
}

if ! run_psql -lqt | cut -d'|' -f1 | tr -d ' ' | grep -qx "$DB_NAME"; then
  info "Creating database '$DB_NAME'"
  if [ "$USE_DOCKER_PG" = 1 ]; then
    docker exec "$PG_CONTAINER" createdb -U "$DB_USER" "$DB_NAME"
  else
    createdb -h "$DB_HOST" -p "$DB_PORT" "$DB_NAME"
  fi
fi
info "Applying migrations to '$DB_NAME'"
for migration in api/migrations/*.sql; do
  run_psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -q < "$migration"
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
  if [ "$TWAIN_EXECUTE_LOCALLY" = 1 ]; then
    info "Runner     → pixi run python -m runner.runner  (EXECUTE on; needs WashU network for the LLM)"
  else
    info "Runner     → pixi run python -m runner.runner  (plan-only; needs WashU network for the LLM)"
  fi
  ( exec pixi run python -m runner.runner ) &
fi

# ── 7. Web app ────────────────────────────────────────────────────────────────
if [ "$RUN_APP" = 1 ]; then
  info "Web app    → http://localhost:3001"
  # EXPO_PUBLIC_AUTH_DISABLED mirrors the API's AUTH_DISABLED so local dev skips
  # the interim login screen (the API injects a dev user regardless).
  ( cd app && EXPO_PUBLIC_API_BASE_URL="http://localhost:$API_PORT" \
      EXPO_PUBLIC_AUTH_DISABLED="true" exec npm run web ) &
fi

wait

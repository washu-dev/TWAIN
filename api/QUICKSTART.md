# TWAIN API — Quick Start

> **Current reference: [`README.md`](README.md)** (all routes, migrations, configuration and deploy). This file is the short local-run guide.

The FastAPI service behind the web UI (conversations, auth, artifacts). It shares
a Postgres database with the **runner**; its `id` for each conversation is the
engine's session id. For the full picture see the repo-root `README.md`
(the "Deploying to AWS" section is the deployment runbook).

## Easiest: the whole stack locally

From the repo root — starts Postgres, applies migrations, and runs the API,
runner, and web app together:
```bash
./dev.sh
```
The API comes up on http://localhost:8000 (docs at `/docs`).

## Just the API

```bash
cd api
python -m venv .venv-api && . .venv-api/bin/activate
pip install -r requirements.txt

# Point at a Postgres instance (see ../.env.example for all variables):
export DB_HOST=localhost DB_PORT=5432 DB_NAME=twaindb DB_USER=postgres DB_PASSWORD=postgres
export AUTH_DISABLED=true        # local dev only — injects a dev admin identity

python main.py                   # applies migrations on startup, serves on :8000
```

The schema is created automatically on startup from `migrations/*.sql`
(idempotent). To run migrations by hand instead:
```bash
python migrate.py                # or: python migrate.py --dry-run
```
Disable startup migrations with `RUN_MIGRATIONS_ON_STARTUP=false`.

## Test it

```bash
curl http://localhost:8000/api/health           # {"status":"ok"}
python -m pytest -q --import-mode=importlib      # the api test suite
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, routes, startup migration hook |
| `conversations.py` | Conversation/message/job data access |
| `auth.py` | Entra SSO + interim login + role checks |
| `database.py` | Postgres connection (creds via `.env` or Secrets Manager) |
| `migrate.py` | Idempotent migration runner (startup + CLI) |
| `migrations/*.sql` | The schema (applied in filename order) |

## Troubleshooting

- **Can't connect to Postgres** — is it running? `./dev.sh` starts one. Check the
  `DB_*` env vars. Run `python ../scripts/preflight.py` for a full readiness check.
- **401s on every route** — auth is on but unconfigured. For local dev set
  `AUTH_DISABLED=true`; for a deploy see the "Auth for a shared/cloud deploy"
  section of the repo-root `README.md`.
- **Port 8000 in use** — `uvicorn main:app --port 8001`.

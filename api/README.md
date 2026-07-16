# TWAIN API

FastAPI backend for the TWAIN web UI. It is a **light request-server**: it
validates auth, does CRUD on Postgres, enqueues runs into the `jobs` table, and
streams progress back to the browser. It never drives the pipeline itself — the
**runner** service claims jobs and runs the engine (see
[`../docs/architecture/web_ui_plan.md`](../docs/architecture/web_ui_plan.md)).

## Setup

```bash
pip install -r requirements.txt          # or use the repo pixi env
cp .env.example .env                      # then edit DB + auth settings
```

The schema lives in [`migrations/`](migrations); apply every file to your
database (the repo-root [`../dev.sh`](../dev.sh) does this automatically):

```bash
for f in migrations/*.sql; do psql -d twaindb -f "$f"; done
```

## Configuration (env)

| Var | Effect |
|---|---|
| `DB_HOST/PORT/NAME/USER/PASSWORD` | Postgres connection |
| `AWS_SECRET_ARN` | if set, DB password is resolved from Secrets Manager instead of `DB_PASSWORD` |
| `AUTH_DISABLED` | `true` for local dev — every request is a dev admin (never in production) |
| `INTERIM_JWT_SECRET` | enables interim email login (`POST /api/auth/login`); the HS256 signing key |
| `INTERIM_ALLOWED_DOMAINS` / `INTERIM_ALLOWED_EMAILS` | who may sign in via interim auth (default domain `wustl.edu`) |
| `ENTRA_TENANT_ID` / `ENTRA_API_AUDIENCE` | Entra (WashU SSO) JWT validation, once SSO is wired up |
| `BOOTSTRAP_ADMIN_EMAILS` | seed admins promoted on first login |

See [`.env.example`](.env.example) for the full list.

## Running

```bash
python main.py           # dev server on http://localhost:8000 (reload on)
# or: uvicorn main:app --host 0.0.0.0 --port 8000
```

Interactive docs: <http://localhost:8000/docs>.

## Endpoints

All endpoints require a valid bearer token except `/api/health` and
`/api/auth/login`. Auth is either an interim session token (HS256) or an Entra
access token (RS256) — both are accepted, routed by algorithm.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness check |
| POST | `/api/auth/login` | interim email sign-in → `{ token, user }` |
| GET | `/api/me` | current user (id, email, name, role) |
| GET | `/api/admin/users` · PATCH `/api/admin/users/{id}/role` | admin: list / set roles |
| POST | `/api/conversations` | start a run from a prompt → enqueues a `start` job |
| GET | `/api/conversations` · `/api/conversations/{id}` | list mine · one with transcript |
| POST | `/api/conversations/{id}/messages` | add a chat / clarification reply |
| POST | `/api/conversations/{id}/approval` | answer the plan-approval gate |
| GET | `/api/conversations/{id}/stream` | **SSE** of pipeline progress (`run_events`) |
| GET | `/api/conversations/{id}/report` | run summary + artifact list |
| GET | `/api/conversations/{id}/artifacts` · `/artifacts/{name}` | list · fetch one artifact |

## Tests

```bash
pytest -q            # unit tests (fakes for DB; no Postgres needed)
ruff check .
```

## Project structure

```
api/
├── main.py            # FastAPI app + routes
├── auth.py            # bearer-token auth: interim (HS256) + Entra (RS256)
├── conversations.py   # conversation/message/artifact/event data access
├── database.py        # Postgres connection + user CRUD (Secrets Manager aware)
├── migrations/        # idempotent SQL schema
├── requirements.txt
└── test_*.py          # test suite
```

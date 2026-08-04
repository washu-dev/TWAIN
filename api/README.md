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

### Report an issue about a run

A researcher can report a problem from the run window without leaving TWAIN; the
run's own data is attached server-side, so a maintainer never has to ask what was
being run. See `run_issue_github.py` (labels + issue body) and `run_issues.py`
(the snapshot + the local record). This is distinct from `POST /api/issues`
(`github_issues.py`), which files a plain title+body issue — that is what the
one-tap "provision this engine" request on the approval card uses.

```
GET  /api/conversations/{id}/issue-context   # exactly what would be attached, + whether GitHub is configured
POST /api/conversations/{id}/issues          # {category, title, description} -> files the issue
GET  /api/conversations/{id}/issues          # what has already been reported for this run
```

`category` is one of `bug | library | result | other` and maps to a GitHub label
(`BugReport`, `LibraryAddition`, `ResultDiscrepancy`, `RunReport`); every issue
also carries `RunReport`. `library` deliberately reuses the tag the pipeline uses
when discovery reaches for an uninstalled library, so both kinds of request
triage as one list.

The response `status` is:

| status | meaning |
|---|---|
| `created` | the issue was filed; `issue_url` points at it |
| `queued` | no GitHub credentials in this deployment — the report is saved against the run, nothing was filed |
| `failed` | GitHub refused or was unreachable; `error` says why. The report is still saved |

Credentials are resolved by `github_issues.py`, which is the single GitHub
identity for the whole API — so configuring the PAT once enables both this and
`POST /api/issues`:

| Env var | Effect |
|---|---|
| `GITHUB_ISSUE_TOKEN` | the PAT. Needs read+write on Issues for the repo. When unset, read from Secrets Manager at `TWAIN_GITHUB_SECRET_ID` (default `TWAIN/github/GITHUB_ISSUE_TOKEN`) |
| `GITHUB_ISSUE_REPO` | `owner/repo` the issues go to (default `washu-dev/TWAIN`). There is no git remote inside the container, so this is configuration-only |
| `TWAIN_RUN_ISSUES=0` | force run reports off, so a staging deployment can't post to the tracker |

Without a PAT the run-report endpoints still work: a report is recorded against
the run with status `queued`. `POST /api/issues` instead returns 502, since there
the user explicitly asked to file an issue.

## Tests

```bash
pytest -q            # unit tests (fakes for DB; no Postgres needed)
ruff check .
```

## Project structure

```
api/
├── main.py               # FastAPI app + routes
├── auth.py               # bearer-token auth: interim (HS256) + Entra (RS256)
├── conversations.py      # conversation/message/artifact/event data access
├── database.py           # Postgres connection + user CRUD (Secrets Manager aware)
├── github_issues.py      # GitHub identity + plain title+body issues (POST /api/issues)
├── run_issue_github.py   # Run reports as GitHub issues: labels + issue body
├── run_issues.py         # The run snapshot attached to a report + its local record
├── migrate.py            # Applies migrations/ on startup (advisory-locked, idempotent)
├── migrations/           # idempotent SQL schema
├── requirements.txt
└── test_*.py             # test suite
```

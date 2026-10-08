# TWAIN API

FastAPI service (Python 3.12, uvicorn on :8000) between the web app, the
database and the worker. It owns the database schema, creates runs and
dispatches their jobs, serves the run's transcript, activity and artifacts,
signs S3 links for Slurm jobs and researchers, and receives RIS webhooks. It
**never runs the pipeline itself**: the worker ([`runner/`](../runner/README.md))
does.

Deployed as ECS Fargate service **`twain-washu`** (cluster `twain-cluster`,
task family `twain-api`), behind an ALB that is the `/api/*` origin of
CloudFront `d1z5umg4xc2bl8.cloudfront.net`. Logs go to `/ecs/twain-api`. The
[architecture diagrams](../docs/architecture/DIAGRAMS_INDEX.md) (07 and 08)
show where it sits.

## Run it

```bash
cd api
AUTH_DISABLED=true DB_HOST=localhost DB_NAME=twaindb DB_USER=postgres DB_PASSWORD=postgres \
  pixi run --manifest-path ../pixi.toml python main.py      # http://localhost:8000/docs
```

Or run everything with `../dev.sh`. The API applies the migrations on startup.

## Routes

**Auth** column:
- **user**: an Entra bearer token (RS256, checked against the tenant JWKS,
  `aud`, `iss` and `exp`) or an interim HS256 token. Conversation routes
  return **404 to anyone but the owner**.
- **admin**: `users.role = admin`.
- `AUTH_DISABLED=true` makes every request a dev admin (local only).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/health` | none | `{status, version, commit}`, the ALB health check |
| GET | `/api/version` | none | `{service, version, commit}` (shown in the app footer) |
| POST | `/api/auth/login` | none | Interim email login (503 unless `INTERIM_JWT_SECRET` is set) |
| GET | `/api/me` | user | The current user, role and notification preferences |
| PUT | `/api/me/notifications` | user | Email preferences: `input`, `approval`, `completed`, `failed`, `terminated` |
| GET | `/api/admin/users` · PATCH `/api/admin/users/{id}/role` | admin | User management |
| GET | `/api/libraries` | user | Library availability, published by the worker from the RIS inventory |
| POST | `/api/issues` | user | A plain GitHub issue (the "provision this engine" request) |
| POST | `/api/conversations` | user | Start a run: `{request, title?, max_cost?}` → enqueues a `start` job |
| GET | `/api/conversations` | user | My runs |
| GET · DELETE | `/api/conversations/{id}` | owner | A run and its transcript · delete it |
| POST | `/api/conversations/{id}/messages` | owner | A chat or clarification reply (enqueues `resume`) |
| POST | `/api/conversations/{id}/approval` | owner | Approve or reject the plan; may override `slurm_request` and `acceptance_metrics` |
| POST | `/api/conversations/{id}/rerun` | owner | Re-run from a stage (`state`, optional feedback, request, Slurm request, metrics) |
| POST | `/api/conversations/{id}/terminate` | owner | Stop the run (and cancel its Slurm job) |
| GET | `/api/conversations/{id}/activity?after=` | owner | In-stage subtasks (`stage.progress`), job log and failure since a cursor, polled every 2 s by the app |
| GET | `/api/conversations/{id}/run-files?attempt=` | owner | Short-lived (1 h) links to a cluster attempt's `bundle` and `outputs` in S3 |
| GET | `/api/conversations/{id}/stream` | owner | SSE of `run_events` (`SSE_POLL_SECONDS`, `SSE_MAX_SECONDS`) |
| GET | `/api/conversations/{id}/report` | owner | Headline result, summary and artifact list |
| GET | `/api/conversations/{id}/artifacts` · `/artifacts/{name}` | owner | List artifacts · fetch one |
| GET · POST | `/api/conversations/{id}/issues` | owner | Run reports filed for this run · file one |
| GET | `/api/conversations/{id}/issue-context` | owner | Exactly what a run report would attach |
| POST | `/api/job-tickets/urls` | job ticket (`X-TWAIN-Ticket`) | A Slurm job swaps its ticket for presigned GET `input/…` and PUT `output/…` links |
| POST | `/api/ris/webhooks` | Standard Webhooks HMAC | RIS API job events → `ris_job_events` (deduplicated, NOTIFY wakes the cluster monitor) |

Every route is a sync `def`, or awaits `run_in_threadpool`. `test_event_loop.py`
fails CI if a route blocks the event loop.

## Modules

| File | Role |
|---|---|
| `main.py` | App, CORS, lifespan (migrations), routes |
| `auth.py` | Entra OIDC validation, interim login, `CurrentUser` / `AdminUser` |
| `database.py` | Postgres connection; DB credentials from Secrets Manager `TWAIN/database/*` (or env with `TWAIN_DB_FROM_ENV=true`); user CRUD |
| `conversations.py` | Runs, messages, artifacts, events, cluster attempts, library availability |
| `dispatch.py` | `TWAIN_DISPATCH=db`: the job is inserted `queued`. `=sqs`: inserted `dispatching` (outbox), then sent to the FIFO queue after commit (group = run, dedup = `job-<id>`) |
| `job_tickets.py` | Hashed, attempt-scoped, expiring job tickets → presigned S3 links (GET only `input/`, PUT only `output/`); also signs the owner's bundle and outputs downloads |
| `ris_webhooks.py` | Verifies the Standard Webhooks signature and timestamp (secret `TWAIN/ris_api/WEBHOOK_SECRET`) and records the event |
| `github_issues.py` · `run_issues.py` · `run_issue_github.py` | GitHub identity, plain issues, run reports |
| `migrate.py` | Applies `migrations/*.sql` in order under an advisory lock, with a `schema_migrations` ledger |
| `version.py` | `TWAIN_VERSION`, then `api/VERSION`, then `dev` |

## Database

`migrations/*.sql` are idempotent and applied at startup
(`RUN_MIGRATIONS_ON_STARTUP=false` to skip; `python migrate.py --dry-run` to
preview).

| Migration | Adds |
|---|---|
| 001 | users, conversations, messages, jobs, run_events, sessions |
| 002 | artifacts (stage specs, bundle files) |
| 003 | NOTIFY on new jobs; `terminate` message kind |
| 004 | job leases and heartbeats (crash recovery); typed conversation lifecycle |
| 005 · 007 | per-user contact · email notification preferences |
| 006 | `cancelling` / `cancelled` |
| 008 | one message kind per question gate |
| 009 | run issue reports |
| 010 · 011 | library availability (+ homepage) |
| 012 | `ris_job_events` (webhooks) + NOTIFY trigger |
| 013 | `job_tickets` |
| 014 | `dispatching` status (SQS outbox) + `cluster_jobs` (detached EXECUTE) |
| 015 | `ris_inventory` (what the RIS envs actually contain) |

## Configuration

| Group | Variables |
|---|---|
| Database | `DB_*` or Secrets Manager `TWAIN/database/*` (`TWAIN_SECRET_PREFIX`, `TWAIN_SECRETS_ROLE_ARN`, `TWAIN_DB_FROM_ENV`), `DB_CONNECT_TIMEOUT`, `PGGSSENCMODE` |
| Auth | `ENTRA_TENANT_ID`, `ENTRA_API_AUDIENCE`, `ENTRA_ISSUER` (or `TWAIN/sso/*`), `BOOTSTRAP_ADMIN_EMAILS`, `INTERIM_JWT_SECRET`, `INTERIM_ALLOWED_DOMAINS`/`_EMAILS`, `AUTH_DISABLED` |
| Dispatch and staging | `TWAIN_DISPATCH`, `TWAIN_JOB_QUEUE_URL`, `TWAIN_RUN_BUCKET` (from repo variables at deploy) |
| RIS webhooks | `RIS_WEBHOOK_SECRET` or `TWAIN_RIS_WEBHOOK_SECRET_ID` |
| GitHub | `GITHUB_ISSUE_TOKEN` (or `TWAIN/github/GITHUB_ISSUE_TOKEN`), `GITHUB_ISSUE_REPO`, `TWAIN_RUN_ISSUES` |
| Other | `SSE_POLL_SECONDS`, `SSE_MAX_SECONDS`, `RUN_MIGRATIONS_ON_STARTUP`, `TWAIN_VERSION`, `TWAIN_GIT_SHA` |

The API's task role, **`twain-api-ecs-task-role`**, can send to the job queue
and read/write `runs/*` in the run bucket (which it needs in order to sign
links). Terraform manages both grants.

## Deploy

`.github/workflows/deploy-api.yml` runs on changes under `api/**`:
1. ruff, pip-audit, and pytest with at least 50% coverage.
2. Build `api/Dockerfile` (python:3.12-slim) with `GIT_SHA`/`VERSION` and push
   it to ECR `twain-ecr`.
3. **Copy the live `twain-api` task definition** and set the image plus
   `TWAIN_DISPATCH`, `TWAIN_JOB_QUEUE_URL` and `TWAIN_RUN_BUCKET` from
   repository variables.
4. Deploy to `twain-washu`, then commit the release and tag `api-v…`.

`api/ecs-task-definition.json` is **not** used by the deploy; it's a historical
reference only. To change the task's environment, add it to the render step of
`deploy-api.yml`.

## Run issue reports

The run window's **Report** button files a GitHub issue with the run's own data
attached, assembled server-side (`run_issues.py`). Before filing,
`issue-context` shows exactly what will be sent.

- **Categories:** `category` is `bug | library | result | other`. They map to
  the labels `BugReport`, `LibraryAddition`, `ResultDiscrepancy` and
  `RunReport`; every issue also carries `RunReport`.
- **Status:** `created` (filed); `queued` (no GitHub credentials, so the report
  is saved against the run); `failed` (GitHub refused, also saved).
- **Credentials:** `github_issues.py` is the single GitHub identity
  (`GITHUB_ISSUE_TOKEN`, a PAT with Issues read/write; `GITHUB_ISSUE_REPO`,
  default `washu-dev/TWAIN`). `TWAIN_RUN_ISSUES=0` turns run reports off.

## Tests

```bash
pixi run --manifest-path ../pixi.toml python -m pytest -q   # fakes for the DB; no Postgres needed
pixi run --manifest-path ../pixi.toml -e lint ruff check .
```

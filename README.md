# TWAIN

TWAIN turns a natural-language research request ("predict the aqueous
solubility of aspirin at 25°C") into a planned, executed, and validated
computational-chemistry run — with a human approval gate before anything is
built or executed.

This README covers everything from local dev to the AWS deploy:

- [Architecture](#architecture) — what the pieces are, locally and in the cloud
- [Quick start](#quick-start-one-command) — run everything locally with one command
- [Running the pieces by hand](#running-the-pieces-by-hand)
- [Configuration flags](#configuration-flags)
- [Tests](#tests)
- [Deploying to AWS](#deploying-to-aws) — the full deployment runbook
- [More documentation](#more-documentation)

## Architecture

```
Browser (localhost:3001)
   │ HTTP
   ▼
Web app (Expo, app/) ──► API (FastAPI, api/, :8000) ──► Postgres (:5432, Docker)
                                                           ▲
                                             polls jobs /  │  writes events & messages
                                                           ▼
                                         Runner (runner/) ──► pipeline (modules/)
                                                           └─► WashU LLM API (needs VPN)
```

The web app never talks to the pipeline directly. The API writes a row into the
`jobs` table (a Postgres-backed queue) and the **runner** — woken instantly by
`LISTEN/NOTIFY` — claims the job and drives the state machine (INTAKE → CLARIFY →
… → PLAN → *approval gate* → BUILD → EXECUTE → … → TERMINATE), writing messages,
progress events, and artifacts back to Postgres for the app to render. When a run
needs the researcher (a clarification, the plan approval, a heavy-calc confirm) it
**suspends** — checkpointed to Postgres, the process released — and a `resume` job
picks it back up when the user replies, so nothing stays pinned waiting on a human.

The same four pieces deploy to AWS (account **730335203321**, region
**us-east-1**):

| Piece | What it is | Deploys to | Workflow |
|-------|------------|-----------|----------|
| **api** | FastAPI: conversations, auth, artifacts | ECS Fargate service `twain-api` (behind an ALB) | `.github/workflows/deploy-api.yml` |
| **runner** | Claims `jobs` rows and drives the pipeline (heavy pixi/GPAW image) | ECS Fargate service `twain-runner` (no inbound) | `.github/workflows/ci-runner.yml` |
| **app** | Expo web UI | S3 `twain-dev-1781888831` + CloudFront `E3PINHJ1G0F5PS` | `.github/workflows/deploy-app.yml` |
| **db** | Shared Postgres (`twaindb`) | RDS `twain-app-database…rds.amazonaws.com` | — (already provisioned) |

## Quick start (one command)

```bash
./dev.sh
```

This starts everything: Postgres (a native server if `psql` can reach one,
otherwise the `twain-pg` Docker container — created on first run, with Colima
started automatically on macOS), the API on :8000 (auth disabled for dev), the
runner (with real execution enabled), and the web app on :3001. First run also
applies DB migrations and installs API/app/pixi dependencies. Ctrl-C stops
everything together.

Variants:

```bash
./dev.sh --no-execute    # plan-only: never runs generated code
./dev.sh --no-app        # backend only (API + runner)
./dev.sh --no-runner     # UI only (chat won't progress past INTAKE)
```

Prerequisites:

- [pixi](https://pixi.sh), Node/npm, and Docker (Docker Desktop, or
  `brew install docker colima` on macOS)
- WashU LLM credentials in the repo-root `.env` (`API_KEY`, `CLIENT_ID`,
  `CLIENT_SECRET`) and the WUSTL VPN — the runner needs both to reach the LLM
  gateway

Then open <http://localhost:3001>, describe a simulation, wait ~30 s for the
proposed execution plan, and hit **Approve & run**. If a conversation seems
idle, check whether it is waiting on your approval before re-prompting. A single
runner drives one slice at a time and serializes work per conversation, but a
suspended run consumes nothing while it waits — so different conversations
progress independently, and you can leave and come back to any of them.

## Running the pieces by hand

Useful when you want each process in its own terminal for separate logs.

**Step 0 — infrastructure** (no terminal stays open):

```bash
colima start           # after a reboot; harmless if already running
docker start twain-pg  # the Postgres container
```

**Terminal 1 — API** (port 8000):

```bash
cd api
AUTH_DISABLED=true DB_HOST=localhost DB_PORT=5432 DB_NAME=twaindb \
  DB_USER=postgres DB_PASSWORD=postgres pixi run python main.py
```

**Terminal 2 — runner** (on the WUSTL VPN):

```bash
export DB_HOST=localhost DB_PORT=5432 DB_NAME=twaindb DB_USER=postgres DB_PASSWORD=postgres
TWAIN_EXECUTE_LOCALLY=1 pixi run python -m runner.runner
```

**Terminal 3 — web app** (port 3001):

```bash
cd app && npm run web
```

## Configuration flags

| Env var | Where | Effect |
|---|---|---|
| `AUTH_DISABLED=true` | API | skip Entra sign-in; every request is a dev admin. Local only. |
| `TWAIN_EXECUTE_LOCALLY=1` | runner | actually run the generated script at EXECUTE (otherwise planning-only) |
| `TWAIN_AUTO_RUN=1` | runner | fully unattended: executes and skips the plan-approval + heavy-calc gates |
| `TWAIN_EXECUTE_SLURM=1` | runner | route all runs to the Compute2 Slurm cluster (the deployment-wide backend; see `runner/README.md`) |
| `TWAIN_VERIFY_CODEGEN=1` | runner | verify + repair generated scripts before running (defaults on when executing) |
| `TWAIN_GITHUB_TOKEN` | pipeline | file `LibraryAddition` install requests as GitHub issues (see below); without it they are ledgered only |
| `TWAIN_GITHUB_REPO` | pipeline | which repo those issues go to (defaults to the git `origin` remote) |
| `TWAIN_LIBRARY_REQUEST_ISSUES` | pipeline | `auto` (default — on iff a token+repo resolve), `1`, or `0` |
| `DB_HOST/PORT/NAME/USER/PASSWORD` | API + runner | Postgres connection (dev defaults: `localhost:5432`, `twaindb`, `postgres`/`postgres`) |

`.env.example` is the single authoritative list of every environment variable
(DB, auth, LLM gateway, runner execution mode, budget rails, app). Cloud values
live in the ECS task definitions + Secrets Manager, not in `.env`.

## Missing libraries → `LibraryAddition` requests

TWAIN only ever plans with libraries it can actually import — the preset set in
`pixi.toml`, mirrored by `configs/discovery_registry.json` and
`configs/calculator_registry.json`. That rule is unchanged: a plan never names a
tool the run can't load.

What used to be silent is now tracked. When method discovery reaches for a
library that isn't installed — or the researcher asks for one by name ("compute
it with VASP") — TWAIN:

1. plans with the best **installed** library instead,
2. tells the researcher on the plan they approve (in `safety_notes`, with the
   library it substituted), and
3. records the ask in `logs/library_requests.json` and files a GitHub issue
   tagged **`LibraryAddition`** so the environment can be extended.

Requests are deduplicated by library name, both locally and against open issues
on GitHub, so a library TWAIN keeps wanting is one issue with a rising
`occurrences` count — not one issue per run. With no token configured the request
is still ledgered and still reported; only the issue is skipped, so nothing here
is required to run TWAIN. See
[`modules/04_method_discovery/library_requests.py`](modules/04_method_discovery/library_requests.py).

A library the researcher names that *is* installed is simply honoured: it goes to
the front of the discovery ranking and is offered to the discovery LLM as a
preference, so a direct ask decides the toolset rather than just being noted.

## Reporting an issue from a run

The run window has a **Report** button, live from the moment a run exists until
long after it ends. It opens a short form — category (Bug / Library / Result /
Other), a title, and what happened — and files a GitHub issue with **the run's own
data attached**: the state it reached, the toolset planning chose, the plan notes
the researcher saw, the errors, the transcript tail, and the names of every
artifact produced. A maintainer never has to ask "what were you running?".

Because submitting publishes run data to the issue tracker, the form shows the
exact snapshot that will be attached before you send it, and says up front when a
deployment has no credentials (the report is then saved against the run instead of
filed). Categories map to GitHub labels; `Library` reuses `LibraryAddition`, so a
researcher asking for a missing package lands in the same bucket as discovery's
own requests. Endpoints, statuses, and deployment config are in
[`api/README.md`](api/README.md).

## More documentation

See [`docs/README.md`](docs/README.md) for the full documentation index. Highlights:

- `docs/project/GETTING_STARTED.md` — project orientation and repo layout
- `runner/README.md` — the runner service: Docker offload, Slurm/Compute2 execution, AWS deploy
- `api/QUICKSTART.md`, `app/QUICKSTART.md` — per-service details
- `docs/backlog/DETAILED_BACKLOG.md` — the story-level backlog

## Tests

```bash
pixi run test            # pipeline unit/contract/integration tests (tests/)
pixi run pytest api/ -q  # API tests
pytest runner/tests -q   # runner tests (no DB or pixi env needed)
```

---

# Deploying to AWS

How to stand up TWAIN's web stack on AWS and exactly what is automated vs.
what you still have to do by hand. Run `make help` for the tooling.

> **Verify as you go:** `python scripts/preflight.py` (local) and
> `python scripts/preflight.py --aws` (cloud) are read-only checks that tell you
> precisely what's ready and what's missing. Use them after every step below.

See the [Architecture](#architecture) table above for what deploys where.

## What's automated now (you don't have to do these)

- **DB schema** — the API applies `api/migrations/*.sql` on startup
  (idempotent, advisory-locked). No manual migration against RDS. Disable with
  `RUN_MIGRATIONS_ON_STARTUP=false`; run by hand with `make migrate`.
- **Readiness checks** — `make preflight` / `make preflight-aws`.
- **LLM secrets + task-def wiring** — `make secrets-apply` creates the three
  Secrets Manager entries from your `.env` and replaces the `-REPLACE`
  placeholders in `runner/ecs-task-definition.json`.
- **Base AWS resources** — `make provision-apply` creates the ECR repos, log
  groups, ECS cluster, and registers both task definitions (idempotent).
- **Build + deploy** — the three GitHub workflows build images/bundles and
  deploy on push to `master` (path-filtered) or via **Run workflow**
  (`workflow_dispatch`).

## Already provisioned in this account

From the committed task definitions: the **RDS** instance + `twaindb`, the **DB
password secret** (`DBPASSWORD-xfmLoq`), the **S3 bucket + CloudFront**, and the
**IAM roles** `ecsTaskExecutionRole` / `ecsTaskRole`. If any of these were torn
down, recreate them first (they're assumed by the task defs and workflows).

## Do this once — the remaining manual steps

You need: the **awscli v2** configured with credentials for account
730335203321, **`gh`** (or the GitHub UI) for repo secrets, and the LLM gateway
creds. Everything below is copy-paste.

### Step 0 — Prove it works locally first (recommended)

Run the [Quick start](#quick-start-one-command) above (`./dev.sh`), start a
simulation, approve the plan, and watch it run. This exercises the exact same
code that runs in cloud. `make preflight` verifies local config.

### Step 1 — Fill `.env` with the LLM gateway credentials

`API_KEY`, `CLIENT_ID`, `CLIENT_SECRET` (see `.env.example`). Required by the
local runner; the cloud runner reads them from Terraform-managed secrets
(Step 2).

### Step 2 — Create the runner secrets and wire the runner task def

```bash
# terraform/secrets.json (git-ignored; template: secrets.example.json) holds the
# LLM creds under secure_api/* and the RIS API PAT under ris_api/TOKEN.
terraform -chdir=terraform apply   # secrets + ECS execution-role read access
terraform -chdir=terraform output runner_secrets_missing   # must be []
make secrets              # PLAN — show the ARNs it will wire
make secrets-apply        # patches the -REPLACE ARNs in the task def
git add runner/ecs-task-definition.json && git commit -m "Wire runner secret ARNs"
```

This is the step that clears the `-REPLACE` placeholders.

### Step 3 — Provision the base AWS resources

```bash
make provision            # PLAN
make provision-apply      # ECR repos + log groups + cluster + task-def registration
```

### Step 4 — Create the two ECS services (once)

Deploys *update* existing services, so they must exist first. You need your VPC
**subnets** and a **security group** that can reach RDS on 5432 (reuse the RDS
VPC/SG). Find them in the RDS console, or:

```bash
aws rds describe-db-instances --query \
  'DBInstances[0].{subnets:DBSubnetGroup.Subnets[].SubnetIdentifier,vpc:DBSubnetGroup.VpcId}' \
  --region us-east-1
```

- **Runner** (no load balancer):
  ```bash
  SUBNETS=subnet-aaa,subnet-bbb SECURITY_GROUPS=sg-xxx \
    scripts/aws/provision.sh --apply     # creates twain-runner if SUBNETS+SG are set
  ```
- **API** (must register with the ALB target group so CloudFront/browsers can
  reach it). `provision.sh` prints the exact `aws ecs create-service` command —
  fill in your subnets, SG, and `targetGroupArn`, then run it. If you don't have
  an ALB + target group yet, create them (target group: HTTP, port 8000, health
  check path `/api/health`, target type `ip`) and point a listener rule at it.

### Step 5 — Set the GitHub repo secrets

| Secret | Used by | Value |
|--------|---------|-------|
| `AWS_ACCESS_KEY_ID` | all 3 workflows | deploy IAM user's key |
| `AWS_SECRET_ACCESS_KEY` | all 3 workflows | deploy IAM user's secret |
| `API_BASE_URL` | app build | public HTTPS URL of the API (the ALB/domain) |

```bash
gh secret set AWS_ACCESS_KEY_ID       # paste when prompted
gh secret set AWS_SECRET_ACCESS_KEY
gh secret set API_BASE_URL --body "https://<your-api-domain>"
```

### Step 6 — Deploy (order matters the first time)

Deploy the **API first** (its startup migration creates the schema the runner
needs), then the runner, then the app.

```bash
gh workflow run deploy-api.yml     # or: push a change under api/
gh workflow run ci-runner.yml      # or: push a change under runner/
gh workflow run deploy-app.yml     # or: push a change under app/
```

### Step 7 — Verify

```bash
python scripts/preflight.py --aws                 # all green?
curl https://<your-api-domain>/api/health         # {"status":"ok"}
```

Open the CloudFront URL, sign in, run a simulation end to end.

## Auth for a shared/cloud deploy

`AUTH_DISABLED=true` is **local-dev only**. For a shared deploy, set one of:

- **Entra SSO** — `ENTRA_TENANT_ID` + `ENTRA_API_AUDIENCE` (add them to the api
  task def `environment`), and the frontend OIDC client id. *(SSO is not wired
  into the frontend yet — see the project plan.)*
- **Interim email login** — `INTERIM_JWT_SECRET` (+ optional
  `INTERIM_ALLOWED_DOMAINS`, `BOOTSTRAP_ADMIN_EMAILS`). Enables
  `POST /api/auth/login`. Good enough for a pilot.

`preflight` warns if neither is configured.

## Rollback

- **api / runner** — ECS keeps prior task-definition revisions. Roll back in the
  console (Update service → pick the previous revision) or re-run the workflow on
  an earlier commit. The DB schema is forward-only + idempotent; a rollback of
  code is safe as long as the older code doesn't need a table a newer migration
  dropped (none do today).
- **app** — re-run `deploy-app.yml` on an earlier commit (it re-syncs S3 +
  invalidates CloudFront).

## Troubleshooting the deploy

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| API task crash-loops on boot | migration can't reach RDS, or bad creds | check the `/ecs/twain-api` logs; verify SG allows the API→RDS on 5432 and `AWS_SECRET_ARN` resolves |
| Chat never leaves **INTAKE** | no runner is claiming jobs | is `twain-runner` running? check `/ecs/twain-runner` logs |
| Runner errors on the LLM call | LLM secrets missing/placeholder | `make secrets-apply`, redeploy the runner; `preflight --aws` flags `-REPLACE` |
| App loads but every call fails | `API_BASE_URL` wrong or CORS | set the `API_BASE_URL` secret + rebuild app; add the app origin to CORS in `api/main.py` |
| `register-task-definition` skipped | `-REPLACE` still in the task def | run Step 2 first |

## Honesty about what's been tested

- The **startup migration**, **preflight**, and **Makefile** are exercised here
  (unit tests + local runs).
- The **AWS scripts** (`setup_secrets.sh`, `provision.sh`) are syntax-checked but
  **not run against AWS from this repo** — review them and run the PLAN mode
  (no `--apply`) first. `setup_secrets.sh` only reads Terraform outputs and
  patches the task def; `provision.sh` is idempotent and check-then-create.
- Creating the **API ECS service + ALB target group** is environment-specific
  (your VPC/subnets/SG/ALB); that piece is documented, not scripted.

---

## More documentation

See [`docs/README.md`](docs/README.md) for the full documentation index. Highlights:

- `docs/project/GETTING_STARTED.md` — project orientation and repo layout
- `runner/README.md` — the runner service: Docker offload, Slurm/RIS, and AWS deploy
- `api/QUICKSTART.md`, `app/QUICKSTART.md` — per-service details
- `docs/backlog/DETAILED_BACKLOG.md` — the story-level backlog

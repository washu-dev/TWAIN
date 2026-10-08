# TWAIN

TWAIN turns a natural-language research request ("predict the aqueous
solubility of aspirin at 25 °C") into a planned, executed and validated
computational-chemistry run. A human approves the plan before anything is
built or executed, and the calculation runs on WashU RIS Compute2 through the
RIS API.

- [Architecture](#architecture): the pieces and how a run flows through them
- [Repository layout](#repository-layout)
- [Run it locally](#run-it-locally)
- [Configuration](#configuration)
- [Tests](#tests)
- [Deploying](#deploying): what happens on merge, and bootstrapping from scratch
- [More documentation](#more-documentation)

## Architecture

Diagrams (open with draw.io, or preview them on GitHub):
- **[`docs/architecture/07_deployment_dependencies.drawio`](docs/architecture/07_deployment_dependencies.drawio)**
  shows every component, folder and dependency across the web app, AWS, GitHub,
  RIS and external services, including what is fetched at build, provisioning
  and run time.
- **[`docs/architecture/08_run_lifecycle.drawio`](docs/architecture/08_run_lifecycle.drawio)**
  follows one run from submission to results: the pizza tracker, the in-stage
  subtasks, emails, the approval gate, the detached Slurm job, and the planned
  observer.

```
 Browser ──► CloudFront d1z5umg4xc2bl8.cloudfront.net
               ├─ /*      → S3 (web app, app/)
               └─ /api/*  → ALB → API (FastAPI, api/, ECS service twain-washu)
                                     │  writes a job row (outbox) + SQS message
                                     ▼
 RDS Postgres (twaindb) ◄──► Worker (runner/, ECS service twain-runner)
   jobs, run_events,          ├─ SQS consumers: run the pipeline (modules/) slice by slice
   cluster_jobs, …            ├─ cluster monitor (one leader): polls Slurm jobs, publishes
                              │  progress, resumes runs, keeps the RIS inventory current
                              └─ LLM gateway (Claude via aiapi.wustl.edu)
                                     │ RIS API (submit / status / output) + webhooks back
                                     ▼
 RIS Compute2: job_wrapper.sh ⇄ S3 run bucket (presigned URLs from the API)
               runs main.py in a shared env under $TWAIN_HOME/twain-envs
```

1. **Submission.** The web app calls `POST /api/conversations`. The API inserts
   a `jobs` row with status `dispatching`, which is an outbox, and sends its id
   to the SQS FIFO queue `twain-jobs.fifo`. A relay re-sends any send that
   failed.
2. **Planning.** A worker claims the job and drives the state machine:
   INTAKE → CLARIFY → DECOMPOSE → DISCOVER → PLAN. Planning uses the latest
   **RIS inventory**, which records what the cluster environments actually
   contain. The run then **suspends** at the approval gate and emails the
   owner. Nothing waits on a human: the run is checkpointed to Postgres, and a
   `resume` job picks it up after the reply.
3. **Building.** BUILD writes the script, using the LLM plus templates. REPAIR
   reviews it and smoke-tests it.
4. **Execution.** EXECUTE uploads the run bundle to S3, issues a one-attempt
   **job ticket**, submits the job through the **RIS API**, and pauses. No
   process holds a Slurm wait.
5. **The job on RIS.** On the compute node the job swaps its ticket for
   presigned S3 links, picks a provisioned environment, runs the smoke test
   and then `main.py`, and uploads its outputs.
6. **Monitoring.** The **cluster monitor** follows the job, woken early by RIS
   webhooks. It streams queue, node and log progress into `run_events`, which
   the app shows as the **pizza tracker** (5 phases) and the in-stage subtask
   checklist. When the job ends, the monitor enqueues the run's resume.
7. **Results.** The worker collects the outputs, then runs INTERPRET →
   VALIDATE (against baselines, Materials Project data and plausibility
   ranges, with CORRECT/REPLAN loops) → ACCEPT → TERMINATE. The owner gets an
   email, and the app shows the report. A failure produces a card with the
   exception, the job's output, download links, a command to reproduce it on
   RIS, and a one-click re-run.

| Component | Path | Runs on | Docs |
|---|---|---|---|
| Web app | [`app/`](app/) | S3 `twain-dev-1781888831` + CloudFront `E3PINHJ1G0F5PS` | [`app/README.md`](app/README.md) |
| API | [`api/`](api/) | ECS Fargate service `twain-washu` (cluster `twain-cluster`) behind an ALB | [`api/README.md`](api/README.md) |
| Worker / runner | [`runner/`](runner/) | ECS Fargate service `twain-runner` (1 vCPU / 4 GB, no inbound) | [`runner/README.md`](runner/README.md) |
| Pipeline | [`modules/`](modules/) | inside the worker | [`modules/README.md`](modules/README.md) |
| RIS side | [`scripts/ris/`](scripts/ris/) | Compute2 (`$TWAIN_HOME` on storage2) | [`scripts/ris/README.md`](scripts/ris/README.md) |
| AWS infrastructure | [`terraform/`](terraform/) | account 730335203321, us-east-1 | [`terraform/README.md`](terraform/README.md) |
| Database | `api/migrations/` | RDS Postgres `twaindb` | [`api/README.md#database`](api/README.md#database) |

## Repository layout

| Path | What it is |
|---|---|
| `app/` | Expo (React Native web) client |
| `api/` | FastAPI service; `api/migrations/*.sql` is the schema, applied at API startup |
| `runner/` | The worker (SQS consumer and cluster monitor) and the shared run-processing code |
| `modules/NN_*/` | The pipeline: intake, decomposition, method discovery, planning, code generation, execution adapters, interpretation, validation, self-correction, provenance and the control plane (state machine and LLM client) |
| `configs/` | Registries (calculators, discovery libraries, intent map), validation baselines, physical ranges, cluster profiles (`clusters/compute2.json`) |
| `schemas/` | JSON Schemas for the stage contracts (intent, goal graph, plan, results, validation, provenance) |
| `scripts/ris/` | Everything that runs on RIS: env specs and builds, the inventory job, the job wrapper, `twain.sh.example` |
| `scripts/` (other) | Versioning (`bump-version.sh`, `release-commit.sh`), AWS helpers, preflight |
| `terraform/` | Secrets, KMS, run-data bucket, job queues, IAM, the worker's ECS service |
| `tests/` | Pipeline unit, contract and integration tests |
| `docs/` | Architecture, decisions, backlog ([index](docs/README.md)) |

## Run it locally

```bash
./dev.sh                 # Postgres + API (:8000, auth off) + runner + web app (:3001)
./dev.sh --no-execute    # plan only: never runs generated code
./dev.sh --no-app        # backend only (API + runner)
./dev.sh --no-runner     # UI only (chat won't progress past INTAKE)
```

`dev.sh` starts Postgres: a native server if `psql` can reach one, otherwise
the `twain-pg` Docker container (created on first run; Colima is started on
macOS). It applies the migrations, installs dependencies on the first run, and
stops everything together on Ctrl-C.

Locally the runner uses the **Postgres queue** (`TWAIN_DISPATCH=db`: the
polling runner claims `queued` rows). The cloud uses SQS (`TWAIN_DISPATCH=sqs`).

Prerequisites:
- [pixi](https://pixi.sh), Node/npm, and Docker (Docker Desktop, or
  `brew install docker colima`).
- LLM gateway credentials in the repo-root `.env` (`API_KEY`, `CLIENT_ID`,
  `CLIENT_SECRET`), and the WUSTL VPN.
- To run on RIS from your machine: `RIS_API_TOKEN` plus `TWAIN_EXECUTE_SLURM=1`.
  See [`runner/README.md`](runner/README.md).

Then open <http://localhost:3001>, describe a simulation, and approve the
plan. To run the pieces in separate terminals:

```bash
cd api && AUTH_DISABLED=true DB_HOST=localhost DB_NAME=twaindb DB_USER=postgres \
  DB_PASSWORD=postgres pixi run python main.py                  # API on :8000
DB_HOST=localhost DB_NAME=twaindb DB_USER=postgres DB_PASSWORD=postgres \
  pixi run python -m runner.runner                              # polling runner
cd app && npm run web                                           # web app on :3001
```

## Configuration

[`.env.example`](.env.example) is the authoritative list of environment
variables. Cloud values live in the ECS task definitions, Secrets Manager
(`TWAIN/*`) and GitHub repository variables, not in `.env`. The most important
ones:

| Variable | Where | Effect |
|---|---|---|
| `AUTH_DISABLED=true` | API | Skip Entra sign-in (every request is a dev admin). Local only. |
| `TWAIN_DISPATCH` | API, worker | `db` (Postgres queue, local) or `sqs` (cloud) |
| `TWAIN_EXECUTE_SLURM=1` | worker | Run on RIS Compute2 (the cloud default) |
| `TWAIN_STAGING=s3`, `TWAIN_RUN_BUCKET` | worker, API | Job files move through S3 with presigned URLs |
| `TWAIN_ENV_FILE` | worker | `twain.sh` on RIS, which jobs source (`CODE_DIR`, `TWAIN_ENVS_ROOT`) |
| `TWAIN_INVENTORY_HOURS` / `_MAX_AGE_HOURS` | worker | How often RIS is inventoried (24 h), and when planning falls back to the specs (168 h) |
| `TWAIN_RUNTIME_REPAIR_ATTEMPTS` | worker | Repair rounds after a script crash (default 2; 0 disables) |
| `TWAIN_NOTIFY_BACKEND`, `TWAIN_NOTIFY_FROM` | worker | `log` or `sendgrid` (plus `ses`/`sns`); the email sender |
| `TWAIN_ENV_APPROVERS` | worker | Who may approve changes to shared RIS environments (default `arifs@wustl.edu`) |
| `TWAIN_AUTO_RUN=1` | runner | Unattended: skips the approval and heavy-calculation gates (testing only) |
| `TWAIN_GITHUB_TOKEN` | pipeline | File `LibraryAddition` issues for libraries TWAIN wanted but doesn't have |

**Missing libraries.** TWAIN plans only with tools it can run. When discovery
wants a library that isn't available, or the researcher names one, it plans
with the best available alternative, says so on the plan card, and records a
`LibraryAddition` request (deduplicated, and filed as a GitHub issue when a
token is configured). See
[`modules/04_method_discovery/library_requests.py`](modules/04_method_discovery/library_requests.py).

**Reporting a run.** The run window's **Report** button files a GitHub issue
with the run's own data attached: the state it reached, the toolset, plan
notes, errors and the transcript tail. The form shows that data before you
send. See [`api/README.md`](api/README.md#run-issue-reports).

## Tests

```bash
pixi run test                     # pipeline: tests/ (unit, contract, integration)
pixi run pytest runner/tests -q   # worker/runner (real-Postgres tests run when one is local)
cd api && pixi run --manifest-path ../pixi.toml python -m pytest -q   # API
cd app && npx tsc --noEmit && npx expo lint                           # web app
```

CI runs all of these on every pull request (see [Deploying](#deploying)).

## Deploying

**Merging to `master` deploys.** Each workflow is path-filtered:

| Workflow | Triggered by | Checks | Deploys |
|---|---|---|---|
| `deploy-api.yml` | `api/**` | ruff, pip-audit, pytest (≥50% coverage) | ECR `twain-ecr` → ECS `twain-washu`; release commit and tag `api-v…` |
| `ci-runner.yml` | `runner/`, `modules/`, `configs/`, `schemas/`, `pixi.*` | ruff, runner tests, `tests/unit` | ECR `twain-runner-ecr` → ECS `twain-runner` |
| `deploy-app.yml` | `app/**` | tsc, expo lint, audit gate, licence check | `expo export` → S3, CloudFront invalidation; release commit and tag `app-v…` |
| `add-to-project.yml` | a new issue | none | Adds it to the TWAIN project board |

- **Versions.** The API and the app each carry an independent version,
  `YYYY.MM.DD.NNN` (`api/VERSION`, `app/VERSION`). It is shown in the app
  footer and at `GET /api/version`.
- **Migrations.** The API applies `api/migrations/*.sql` at startup
  (idempotent, under an advisory lock). Deploy the API before code that needs
  a new table.
- **Settings without a code change:** repository variables `TWAIN_DISPATCH`,
  `TWAIN_JOB_QUEUE_URL`, `TWAIN_RUN_BUCKET`, `TWAIN_ENV_FILE`,
  `TWAIN_NOTIFY_BACKEND`, `TWAIN_NOTIFY_FROM` and `TWAIN_ENV_APPROVERS`. Change
  one, then re-run the matching workflow.
- **Secrets:** repository secrets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `API_BASE_URL` and `ADD_TO_PROJECT_PAT`. Runtime secrets live in Secrets
  Manager under `TWAIN/*`, managed by Terraform from the git-ignored
  `terraform/secrets.json`.
- **Infrastructure:** `terraform -chdir=terraform plan` / `apply` (see
  [`terraform/README.md`](terraform/README.md)). **WashU IT denies deleting
  or detaching IAM role policies**, so IAM changes must only add.

**Rollback.**
- API or worker: ECS keeps earlier task-definition revisions; update the
  service to the previous one, or re-run the workflow on an earlier commit.
- App: re-run `deploy-app.yml` on an earlier commit.
- The database schema only moves forward, and every migration is idempotent.
- A RIS environment: `scripts/ris/rebuild_envs.sh rollback <env> <version>`.

**Troubleshooting.**

| Symptom | Look at |
|---|---|
| The chat never leaves INTAKE | `/ecs/twain-runner`: is the worker up (`worker up: …`)? Is SQS sending working (`/ecs/twain-api`, "SQS send … failed")? |
| A run fails on RIS | The failure card: exception, job stdout/stderr, Reproduce on RIS. Exit 3 = `twain.sh`/`CODE_DIR`, 4 = stale checkout (`git -C $CODE_DIR pull`), 6 = bundle download, 7 = output upload |
| Plans pick environments that don't exist | `SELECT taken_at, status FROM ris_inventory ORDER BY id DESC LIMIT 3;`, and the worker log line `planning from …` |
| No emails | Repository variables `TWAIN_NOTIFY_BACKEND=sendgrid` and `TWAIN_NOTIFY_FROM`, the `TWAIN/sendgrid/API_KEY` secret, and the user's notification preferences |

**Bootstrapping from scratch** (already done for this account): `make preflight`
/ `make preflight-aws` check readiness. `scripts/aws/provision.sh` creates the
ECR repositories, log groups, the ECS cluster and the API service (with its ALB
target group: HTTP 8000, health check `/api/health`). Terraform creates
secrets, the bucket, the queues, IAM and the worker service. Deploy the API
first: its migrations create the schema.

**Auth.** The app signs in with Microsoft Entra ID (OIDC auth-code + PKCE),
and the API validates the tokens (`ENTRA_TENANT_ID`, `ENTRA_API_AUDIENCE`).
`AUTH_DISABLED` is for local development only. An interim email login
(`INTERIM_JWT_SECRET`) exists for pilots.

## More documentation

- [`docs/README.md`](docs/README.md): the documentation index (architecture, decisions, backlog)
- [`docs/architecture/DIAGRAMS_INDEX.md`](docs/architecture/DIAGRAMS_INDEX.md): every diagram, including 07 and 08
- Component READMEs: [app](app/README.md) · [api](api/README.md) ·
  [runner](runner/README.md) · [modules](modules/README.md) ·
  [scripts/ris](scripts/ris/README.md) · [terraform](terraform/README.md)

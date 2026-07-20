# TWAIN Deployment Runbook

How to stand up TWAIN's web stack — locally and on AWS — and exactly what is
automated vs. what you still have to do by hand. Run `make help` for the tooling.

> **Verify as you go:** `python scripts/preflight.py` (local) and
> `python scripts/preflight.py --aws` (cloud) are read-only checks that tell you
> precisely what's ready and what's missing. Use them after every step below.

---

## 1. Architecture (what deploys where)

| Piece | What it is | Deploys to | Workflow |
|-------|------------|-----------|----------|
| **api** | FastAPI: conversations, auth, artifacts | ECS Fargate service `twain-api` (behind an ALB) | `.github/workflows/deploy-api.yml` |
| **runner** | Claims `jobs` rows and drives the pipeline (heavy pixi/GPAW image) | ECS Fargate service `twain-runner` (no inbound) | `.github/workflows/ci-runner.yml` |
| **app** | Expo web UI | S3 `twain-dev-1781888831` + CloudFront `E3PINHJ1G0F5PS` | `.github/workflows/deploy-app.yml` |
| **db** | Shared Postgres (`twaindb`) | RDS `twain-app-database…rds.amazonaws.com` | — (already provisioned) |

The api and runner share the database. The api records your chat turns and
enqueues a job; the runner claims it and drives `Orchestrator.run(...)`, pausing
at the plan-approval gate. AWS account **730335203321**, region **us-east-1**.

---

## 2. What's automated now (you don't have to do these)

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

## 3. Already provisioned in this account

From the committed task definitions: the **RDS** instance + `twaindb`, the **DB
password secret** (`DBPASSWORD-xfmLoq`), the **S3 bucket + CloudFront**, and the
**IAM roles** `ecsTaskExecutionRole` / `ecsTaskRole`. If any of these were torn
down, recreate them first (they're assumed by the task defs and workflows).

---

## 4. Do this once — the remaining manual steps

You need: the **awscli v2** configured with credentials for account
730335203321, **`gh`** (or the GitHub UI) for repo secrets, and the LLM gateway
creds. Everything below is copy-paste.

### Step 0 — Prove it works locally first (recommended)
```bash
cp .env.example .env          # fill API_KEY / CLIENT_ID / CLIENT_SECRET
./dev.sh                      # DB + API + runner + app on localhost
```
Open http://localhost:8081, start a simulation, approve the plan, see it run.
This exercises the exact same code that runs in cloud. `make preflight` verifies
local config.

### Step 1 — Fill `.env` with the LLM gateway credentials
`API_KEY`, `CLIENT_ID`, `CLIENT_SECRET` (see `.env.example`). Required by both
the local runner and `make secrets-apply`.

### Step 2 — Create the LLM secrets and wire the runner task def
```bash
make secrets              # PLAN — see what it will do
make secrets-apply        # creates 3 Secrets Manager entries + patches the task def
git add runner/ecs-task-definition.json && git commit -m "Wire LLM secret ARNs"
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

---

## 5. Auth for a shared/cloud deploy

`AUTH_DISABLED=true` is **local-dev only**. For a shared deploy, set one of:
- **Entra SSO** — `ENTRA_TENANT_ID` + `ENTRA_API_AUDIENCE` (add them to the api
  task def `environment`), and the frontend OIDC client id. *(SSO is not wired
  into the frontend yet — see the project plan.)*
- **Interim email login** — `INTERIM_JWT_SECRET` (+ optional
  `INTERIM_ALLOWED_DOMAINS`, `BOOTSTRAP_ADMIN_EMAILS`). Enables
  `POST /api/auth/login`. Good enough for a pilot.

`preflight` warns if neither is configured.

---

## 6. Rollback

- **api / runner** — ECS keeps prior task-definition revisions. Roll back in the
  console (Update service → pick the previous revision) or re-run the workflow on
  an earlier commit. The DB schema is forward-only + idempotent; a rollback of
  code is safe as long as the older code doesn't need a table a newer migration
  dropped (none do today).
- **app** — re-run `deploy-app.yml` on an earlier commit (it re-syncs S3 +
  invalidates CloudFront).

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| API task crash-loops on boot | migration can't reach RDS, or bad creds | check the `/ecs/twain-api` logs; verify SG allows the API→RDS on 5432 and `AWS_SECRET_ARN` resolves |
| Chat never leaves **INTAKE** | no runner is claiming jobs | is `twain-runner` running? check `/ecs/twain-runner` logs |
| Runner errors on the LLM call | LLM secrets missing/placeholder | `make secrets-apply`, redeploy the runner; `preflight --aws` flags `-REPLACE` |
| App loads but every call fails | `API_BASE_URL` wrong or CORS | set the `API_BASE_URL` secret + rebuild app; add the app origin to CORS in `api/main.py` |
| `register-task-definition` skipped | `-REPLACE` still in the task def | run Step 2 first |

---

## 8. Environment variables

`.env.example` is the single authoritative list (DB, auth, LLM gateway, runner
execution mode, budget rails, app). Cloud values live in the ECS task
definitions + Secrets Manager, not in `.env`.

---

## Appendix — honesty about what's been tested

- The **startup migration**, **preflight**, and **Makefile** are exercised here
  (unit tests + local runs).
- The **AWS scripts** (`setup_secrets.sh`, `provision.sh`) are syntax-checked but
  **not run against AWS from this repo** — review them and run the PLAN mode
  (no `--apply`) first. They're idempotent and check-then-create.
- Creating the **API ECS service + ALB target group** is environment-specific
  (your VPC/subnets/SG/ALB); that piece is documented, not scripted.

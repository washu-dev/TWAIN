# TWAIN Runner Service

Drives the pipeline for the web UI. It claims queued jobs from the Postgres
`jobs` table, constructs an `Orchestrator`, and streams progress back into the
shared database (`run_events`, `messages`, `conversations`, `sessions`) that the
API reads. See `docs/architecture/web_ui_plan.md` §4.

Why a separate service (not the API): the pipeline needs the heavy pixi
environment (`pymatgen`, `ase`, …), the WashU LLM credentials, and runs for
minutes — and it **blocks** on the user during CLARIFY and at the plan-approval
gate. One job == one run; run several runner processes for more concurrency.

## How a run flows
1. API `POST /api/conversations` inserts a conversation + first message + a
   `start` job.
2. The runner claims the job and runs `orchestrator.run(until=BUILD)` — intake,
   clarify (blocking on the user via `messages`), decompose, discover, plan.
3. It pauses at BUILD (plan generated, nothing built), posts the plan as an
   `approval_request`, and blocks for the user's `POST /approval`.
4. On **approve** it runs to completion; on **reject** it stops before building.
5. Throughout, an event sink writes `run_events` (tailed by the SSE endpoint)
   and mirrors `current_state` / `status` onto the conversation.

## Run locally
Requires a reachable Postgres with the schema from `api/migrations/001_web_ui.sql`
applied, plus the WashU LLM credentials in `.env` (`API_KEY`, `CLIENT_ID`,
`CLIENT_SECRET`).

```bash
# refresh the lock after the psycopg2/boto3 additions (one time)
pixi install

export DB_HOST=localhost DB_PORT=5432 DB_NAME=twaindb DB_USER=postgres DB_PASSWORD=...
pixi run python -m runner.runner --once   # process one job and exit
pixi run python -m runner.runner          # loop forever
```

## Running the generated calculators (the `sim` env)
The DFT calculators (GPAW, DFTB+) have no Python-3.12 conda build, so they live
in a separate Python-3.11 pixi environment, **`sim`**. Discovery picks a
calculator that actually builds for the current platform — **GPAW on linux-64,
DFTB+ on both linux-64 and osx-arm64** (macOS) — so a generated band-gap script
runs under `sim`:

```bash
pixi install -e sim                                  # one time (heavy)
pixi run -e sim python <bundle>/main.py --smoke      # build structure + check the binary
pixi run -e sim python <bundle>/main.py              # real run
```

To drive the whole pipeline (so EXECUTE runs the calculation for real), run the
runner under `-e sim` on a supported platform **and set `TWAIN_EXECUTE_LOCALLY=1`**
(off by default, so the runner is planning-only until you opt in). The same flag
works locally in Docker and on ECS, so the calculation runs identically in both:

```bash
# real GPAW DFT band gap on a linux-64 host (or inside the Linux runner container):
TWAIN_EXECUTE_LOCALLY=1 pixi run -e sim python -m runner.runner --once
```

`TWAIN_VERIFY_CODEGEN` (defaults to the execute flag) turns on the REPAIR stage's
LLM verify/repair of the generated script before the real run.

For a **fully unattended run** set **`TWAIN_AUTO_RUN=1`**: it implies execution and
auto-approves *both* human gates — the plan-approval pause and the heavy-calc
"run it now?" confirmation — so TWAIN goes intake → discover → plan → build →
**runs the calculation automatically** with no interaction:

```bash
TWAIN_AUTO_RUN=1 pixi run -e sim python -m runner.runner --once
```

(CLARIFY still pauses only if the request is genuinely ambiguous — an unattended
deployment should send unambiguous requests.) On ECS, set `TWAIN_AUTO_RUN=1` in
the task definition for a hands-off runner.

**DFTB+ needs Slater-Koster parameter files** (`.skf`) for the system's elements:
download a set (e.g. `pbc` for silicon) from <https://dftb.org> and point
`DFTB_PREFIX` at its directory. The generated script says so if it's unset.
GPAW's PAW datasets ship with the conda package (no extra step).

## Test
No DB or pixi env needed — the unit tests use in-memory fakes:

```bash
pytest runner/tests -q
ruff check runner
```

## Deploy (AWS)
The runner runs as its own ECS/Fargate service (`twain-runner`) on **linux-64**, so
the `sim` env ships **GPAW** and a band-gap job computes a real DFT result. CI job
`deploy` in `.github/workflows/ci-runner.yml` builds `runner/Dockerfile` **from the
repo root** → pushes to ECR → deploys `runner/ecs-task-definition.json` to the ECS
service. It runs on push to `master` (or `workflow_dispatch`).

One-time prerequisites (the deploy job assumes these exist):
1. **ECR repo** `twain-runner-ecr`.
2. **ECS service** `twain-runner` on cluster `twain-cluster` (Fargate; no load
   balancer — it's a worker). Size for DFT: the task def uses 2 vCPU / 8 GB.
3. **Secrets Manager** entries for the WashU LLM creds, wired into the task def's
   `secrets` block (replace the `…-REPLACE` ARNs): `API_KEY`, `CLIENT_ID`,
   `CLIENT_SECRET`. The DB password is already the shared `AWS_SECRET_ARN`.
4. Repo secrets `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (same as the API).
5. Task role: RDS access + `secretsmanager:GetSecretValue` for those ARNs.

Execution mode (env in the task def):
- **`TWAIN_EXECUTE_LOCALLY=1`** (the default set here) — run calculations for real,
  keeping the web-UI plan-approval step. This is what computes the GPAW band gap.
- **`TWAIN_AUTO_RUN=1`** — fully unattended: also skip the plan-approval and
  heavy-calc gates. Swap it in if you want hands-off runs with no approval click.

The image is identical locally and on ECS, so a run behaves the same in Docker and
in Fargate. EXECUTE runs generated code, so keep the task sandboxed (resource
limits, minimal IAM/network). Local build to sanity-check before pushing:

```bash
docker build -f runner/Dockerfile -t twain-runner .   # heavy: full scientific stack + GPAW
```

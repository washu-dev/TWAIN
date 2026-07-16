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
in a separate Python-3.11 pixi environment, **`sim`**. Discovery plans against
the richest platform it can *reach*: **linux-64 when a Docker daemon is available**
(the runner image), else the host. So it picks **GPAW** (linux-64-only, higher
fidelity) whenever Docker is present — even on a Mac, where the run is offloaded
to the container (see "Automatic Docker offload" below) — and falls back to
**DFTB+** (builds natively on both linux-64 and osx-arm64) when Docker is absent.
A generated band-gap script runs under `sim`:

```bash
pixi install -e sim                                  # one time (heavy)
pixi run fetch-slako                                 # one time: DFTB+ .skf params -> slako/
pixi run -e sim python <bundle>/main.py --smoke      # build structure + real single-point smoke
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

**DFTB+ needs Slater-Koster parameter files** (`.skf`) — the conda package ships
only the binary. Run **`pixi run fetch-slako`** once: it downloads the `mio`
(H, C, N, O, S, P) and `tiorg` (adds Ti; bulk Ti / TiO2) sets from the
`dftbparams` GitHub org into `slako/`, and pixi's `[activation.env]` points
`DFTB_PREFIX` there automatically — for both the `sim` env and the runner
process, so a direct `pixi run -e sim python <bundle>/main.py` and a full
pipeline run both resolve the files. The image bakes them in at build time
(`runner/Dockerfile`). Other element sets (e.g. `pbc` / `siband` for silicon) can
be dropped into the same `slako/` dir from <https://dftb.org> or `dftbparams`.
GPAW's PAW datasets ship with the conda package (no extra step).

## Automatic Docker offload (run TWAIN on your Mac, execute Linux engines in Docker)
You don't have to run the *whole* pipeline in Docker to use a Linux-only engine.
Run TWAIN natively (e.g. `pixi run python modules/07_runtime_orchestrator/orchestrator.py`)
and it will **offload just the heavy calculation** to the container when the
selected engine has no build for your host:

- **Discovery** plans against `linux-64` when a Docker daemon is reachable
  (`method_discovery.calculator_registry.planning_platform`), so GPAW is selected
  on a Mac instead of silently downgrading to DFTB+. The plan says so explicitly.
- **EXECUTE** routes a calculator that `needs_docker` (no host build, but a
  linux-64 build exists) to `DockerExecutionAdapter`, which runs the bundle via
  `docker run --platform linux/amd64 … twain-runner pixi run -e sim python …`,
  bind-mounting the bundle so `results.csv` lands back on the host.
- If the daemon isn't reachable, discovery falls back to the best **native**
  engine (DFTB+) and notes that installing Docker would enable a higher-fidelity
  run. If the daemon is up but the image isn't built, EXECUTE skips gracefully
  with the exact `docker build` command (it never builds the image for you).

Prerequisites: Docker set up (see "One-time Docker setup" below) and the image
built once (`docker build --platform linux/amd64 -f runner/Dockerfile -t
twain-runner .`). Set `TWAIN_NO_DOCKER=1` to force host-only planning;
`TWAIN_DOCKER_IMAGE` / `TWAIN_DOCKER_PLATFORM` override the image tag / platform.

## Run in Docker (any OS)
Some calculators only build on Linux (GPAW has no Windows or Apple-Silicon
build). The runner image is a full `linux-64` TWAIN — **both** pixi envs
(default + `sim`) are baked in at build time — so Docker is the way to run the
complete stack on a Mac or Windows machine.

**One-time Docker setup**
- **macOS**: Docker Desktop, or the lighter CLI route:
  `brew install docker docker-buildx colima`, add
  `"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]` to
  `~/.docker/config.json`, then `colima start --vm-type vz --vz-rosetta`
  (Rosetta runs the amd64 image at near-native speed; `colima start` again
  after each reboot).
- **Windows**: Docker Desktop with the WSL2 backend — `linux-64` containers run
  natively, no emulation.

**Build and run** (build context is the repo ROOT; on Apple Silicon the
`--platform` flag is required, since the image is `linux-64` only):

```bash
docker build --platform linux/amd64 -f runner/Dockerfile -t twain-runner .

# sanity check: the calculators are really in the image
docker run --rm --platform linux/amd64 twain-runner \
  .pixi/envs/sim/bin/python -c "import ase, gpaw; print(gpaw.__version__)"

# unattended full run (mount logs/ so results survive the container)
docker run --rm --platform linux/amd64 --env-file .env -e TWAIN_AUTO_RUN=1 \
  -e DB_HOST=host.docker.internal -v "$PWD/logs:/app/logs" twain-runner
```

## Run on the Slurm cluster (WashU RIS Compute2)
When a run exceeds what your laptop (or the Docker route) should carry, EXECUTE
can submit the built RunBundle to the school's HPC cluster instead
(Story 5.4). Set `TWAIN_EXECUTE_SLURM=1` (runner/env) or pass `--slurm` to the
orchestrator CLI:

```bash
pixi run python modules/07_runtime_orchestrator/orchestrator.py --slurm
```

What happens at EXECUTE:
1. **stage** — the bundle is rsynced to
   `<storage_root>/twain-runs/<session_id>/` on the cluster
   (`configs/clusters/compute2.json` points at the writable allocation dir);
2. **submit** — an `#SBATCH` script is rendered from the plan's `slurm_request`
   (partition auto-selected from CPU/GPU/wall-time; `ml load ris slurm`) and
   submitted on a login node over SSH;
3. **wait** — `squeue`/`sacct` are polled (bounded). If the wait budget expires
   the job is **left running** and the result says how to check on it
   (`squeue --job <id>`) — a multi-hour job is never killed just because our
   wait was shorter;
4. **fetch** — outputs (`results.csv`, the job log) are rsynced back into the
   session's artifacts dir, and `sacct` Elapsed/MaxRSS land on the execution
   result for provenance.

The job builds its own venv from the bundle's `requirements.txt` (compute
nodes have no TWAIN environment), and the smoke test runs first so a missing
dependency fails in seconds instead of after a long queue wait.

Prerequisites and knobs:
- WashU VPN (AnyConnect) + Duo, and an SSH key for the login node
  (`ssh <wustl-key>@c2-login-001.ris.wustl.edu` must work non-interactively).
- `TWAIN_SLURM_USER` — your WUSTL key (omit if `~/.ssh/config` handles it);
  `TWAIN_SLURM_HOST` — override the login node, or set it to the empty string
  when the process already runs *on* a login node (no SSH hop);
  `TWAIN_SLURM_CLUSTER` / `--cluster` — another `configs/clusters/` profile.

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
limits, minimal IAM/network). To sanity-check the build before pushing, see
"Run in Docker (any OS)" above (add `--platform linux/amd64` on Apple Silicon).

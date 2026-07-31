# TWAIN Runner Service

Drives the pipeline for the web UI. It claims queued jobs from the Postgres
`jobs` table, constructs an `Orchestrator`, and streams progress back into the
shared database (`run_events`, `messages`, `conversations`, `sessions`) that the
API reads. See `docs/architecture/web_ui_plan.md` §4.

Why a separate service (not the API): the pipeline needs the heavy pixi
environment (`pymatgen`, `ase`, …), the WashU LLM credentials, and runs for
minutes. When it needs the researcher it **suspends** rather than blocks — the
run is checkpointed and the process is released — so one runner serves many runs
and nothing spins waiting on a human.

## How a run flows (async suspend/resume)
A *job* is one slice of a run, not a whole run. A run advances until it needs the
user, then releases the process; a `resume` job continues it when the user
responds. No thread is ever pinned to a waiting run.

1. API `POST /api/conversations` inserts a conversation + first message + a
   `start` job.
2. The runner claims the job and runs `orchestrator.run(until=BUILD)` — intake,
   clarify, decompose, discover, plan.
3. If CLARIFY needs an answer, the `ask` bridge posts the question, marks the run
   `awaiting_input`, **suspends** (raises `SuspendRun`), and the runner returns.
   The user's `POST /messages` reply enqueues a `resume` job that re-drives the
   run — the same `ask` now returns the answer and the pipeline continues.
4. At BUILD (plan generated, nothing built) it posts the plan as an
   `approval_request`, marks the run `awaiting_approval`, and releases. The
   user's `POST /approval` enqueues a `resume`: **approve** crosses the gate and
   runs to completion; **reject** stops before building. (The heavy-calc "run it
   now?" confirmation during EXECUTE suspends/resumes the same way.)
5. Throughout, an event sink writes `run_events` (tailed by the SSE endpoint)
   and mirrors `current_state` / `status` onto the conversation.
6. **Terminate** (the button in the chat header) posts
   `POST /api/conversations/{id}/terminate`, which records a `terminate`
   control message and flips the status to `cancelling`. The runner polls for
   it between stages, inside the clarify/approval waits, and between Slurm
   `squeue` polls (where it also `scancel`s the cluster job), then settles the
   conversation as `cancelled` instead of `error`.

**Waking the runner** — instead of polling every second, the runner `LISTEN`s on
the `twain_jobs` channel; a trigger (`api/migrations/003_job_notify.sql`)
`NOTIFY`s it the instant a job is queued, so a released runner wakes immediately.
A generous fallback poll (`--poll`, default 30s) covers any missed notification.

**Notifications** — on each suspend the run reaches out to the *owner* who left
it (resolved from `users` via the conversation; see `db.owner_contact`) so they
can return when ready. Configure via `TWAIN_NOTIFY_BACKEND` (`log` default, or
`ses`/`sendgrid` email / `sns` SMS); email targets the owner's address and `sns`
texts their `phone` (migration `005_user_contact.sql`), each falling back to the
configured global `TWAIN_NOTIFY_EMAIL` / `TWAIN_NOTIFY_SNS_TOPIC_ARN` when the
owner has no contact on file. See `runner/notifications.py`. `TWAIN_APP_URL` adds
a deep link back to the run.

**Resume durability** — a run's state + context resume from the Postgres session
store, and its stage artifacts (intent_spec, execution_plan, the generated run
bundle, …) are durable too: `capture_artifacts` writes their contents to the
`artifacts` table every slice, and `rehydrate_artifacts` restores them to local
disk before a resume drives the run (see `runner/artifacts.py`). So any runner
can resume any run — even on a fresh box, or after `logs/` was cleaned — with no
shared `logs/` volume required. (Outputs produced within the final, non-suspending
slice aren't needed to resume.) Concurrency is safe regardless — `claim_job`
serializes jobs per session and a redundant `resume` is a no-op.

**Crash recovery** — a claimed job is kept alive by a heartbeat (`jobs.heartbeat_at`)
while the runner works. If a runner dies mid-slice (OOM, redeploy, SIGKILL) its
heartbeat goes stale; after the lease (`TWAIN_JOB_LEASE_SECONDS`, default 600s)
any runner's reaper re-queues the job so it re-drives from the checkpoint, or
dead-letters it once it has been attempted `TWAIN_JOB_MAX_ATTEMPTS` times (default
3) and posts a failure message. The lease only has to outlast a few missed
heartbeats (`TWAIN_JOB_HEARTBEAT_SECONDS`, default 60s), **not** the longest slice
— a healthy multi-hour EXECUTE keeps beating — so recovery after a real crash
takes about one lease, not hours. In-process failures (a VPN/LLM blip, a stage
timeout) are retried the same way before the run is failed. Without this, a
crashed runner left its job stuck `running` forever and, because of the
per-session serialization above, permanently blocked every future `resume` for
that session.

## Run locally
Requires a reachable Postgres with the schema from `api/migrations/001_web_ui.sql`
applied, plus the WashU LLM credentials in `.env` (`API_KEY`, `CLIENT_ID`,
`CLIENT_SECRET`). Optional: `MP_API_KEY` (free key from
[materialsproject.org/api](https://materialsproject.org/api)) enables
database-retrieval tasks — prompts that ask to *look up* a stored value from the
Materials Project rather than compute it. Without the key such runs fail fast at
BUILD with a message saying to set it.

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
(Story 5.4). Pick **"RIS cluster (Slurm)"** in the web app's "Run on" selector
when starting a run (it rides in the job's `compute_target` param), set
`TWAIN_EXECUTE_SLURM=1` to make it the runner-wide default, or pass `--slurm`
to the orchestrator CLI:

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

**Compiled calculators (GPAW, xtb, DFTB+) can't be pip-installed by the job**
— GPAW needs libxc headers, xtb-python isn't on PyPI at all. For those,
shared environments live under the profile's `envs_root`
(`/storage2/fs1/mdan/Active/dtrc2026-workshop/twain-envs` on compute2); the
job automatically prefers `<envs_root>/<calculator>/bin/python` (then
`<envs_root>/<tool>/`, then `<envs_root>/default/`) over building a venv —
selecting the first env that passes the bundle's smoke test, so an env that
exists but lacks an import never silently wins.

**The envs are declarative.** Each env has a version-controlled spec in
`scripts/ris/envs/<name>.yml` (the env is named after the spec file and
matched case-insensitively against the plan's calculator / tool name, so
`gpaw.yml` -> `twain-envs/gpaw` serves any plan that selects GPAW). Provision
or sync them on a login node — micromamba needs no modules or sudo, and the
script installs it if missing:

```bash
ssh <wustl-key>@c2-login-001.ris.wustl.edu
cd /storage2/fs1/mdan/Active/dtrc2026-workshop/twain-backend
bash scripts/ris/provision_envs.sh           # all specs
bash scripts/ris/provision_envs.sh default   # just one env
```

To add a package (a new plan needs an import the env lacks — the smoke test
names it in the job log, and the pre-submit preflight names it before
queueing), **edit the spec, commit, and rerun the script**. Never
`micromamba install` into a shared env by hand: manual drift is how
`twain-envs/default` silently lost rdkit, and hand edits also race against
teammates' running jobs.

To verify an env exactly the way the Slurm job invokes it (no activation;
`OPAL_PREFIX` tells OpenMPI where its runtime data lives — always use the
ABSOLUTE path, a relative mpirun path breaks OpenMPI's prefix
auto-detection):

```bash
ROOT=/storage2/fs1/mdan/Active/dtrc2026-workshop
"$ROOT/twain-envs/gpaw/bin/python" \
  -c "import gpaw, ase, pymatgen, spglib; print(gpaw.__version__)"
OPAL_PREFIX="$ROOT/twain-envs/gpaw" \
  "$ROOT/twain-envs/gpaw/bin/mpirun" --version | head -1
```

**Before submitting**, the adapter also runs a preflight from the login node:
each candidate env is probed with the bundle's smoke test, and if none passes
it asks pip (`--dry-run`) whether the job's venv fallback could even install
the requirements. A definite "no matching distribution" verdict fails the run
immediately with a pointer to the env specs — instead of after staging plus a
queue wait.

Prerequisites and knobs:
- WashU VPN (AnyConnect) + Duo, and an SSH key for the login node
  (`ssh <wustl-key>@c2-login-001.ris.wustl.edu` must work non-interactively).
- `TWAIN_SLURM_USER` — your WUSTL key (omit if `~/.ssh/config` handles it);
  `TWAIN_SLURM_HOST` — override the login node, or set it to the empty string
  when the process already runs *on* a login node (no SSH hop);
  `TWAIN_SLURM_CLUSTER` / `--cluster` — another `configs/clusters/` profile.

## Deploy the backend ON RIS (runner on the login node)

Instead of running the runner on your laptop (VPN required, laptop must stay
awake), deploy it to the cluster itself. It polls the same shared Postgres on
AWS RDS, so the web UI and API stay exactly where they are — only the runner
moves. On the login node it submits `sbatch` directly (no SSH hop, no VPN in
the loop) and stages bundles with plain local copies.

One-time, from your workstation (on the VPN):

```bash
cp scripts/ris/env.ris.example .env.ris   # fill in LLM creds + the RDS password
RIS_USER=<your-wustl-key> scripts/ris/deploy.sh
```

This rsyncs the repo to `<team storage>/twain-backend`, installs pixi + the
default env there, and verifies connectivity (RDS :5432, LLM gateway, sbatch).
Then start the runner in a tmux session on the login node:

```bash
ssh <your-wustl-key>@c2-login-001.ris.wustl.edu
tmux new -s twain-runner
bash /storage2/fs1/mdan/Active/dtrc2026-workshop/twain-backend/scripts/ris/start_runner.sh
```

Detach with `Ctrl-B d`; the runner keeps running and auto-restarts on crashes.
Redeploy code changes by re-running `deploy.sh` and restarting the loop — or
turn on auto-update (below) and never do it by hand again.

### On-demand extra workers (backlog behind a long run)

The runner drives one job at a time, and EXECUTE holds it for as long as the
Slurm job runs — so a multi-hour DFT run makes everyone else's jobs queue
even though the cluster has free nodes. `scale_runners.sh` fixes that by
keeping up to N one-shot workers alive (`runner.runner --once`: claim one
job, drive it, exit) while a claimable backlog exists, then exiting once the
queue drains. Make it automatic with a one-time cron install on the login
node:

```bash
bash /storage2/fs1/mdan/Active/dtrc2026-workshop/twain-backend/scripts/ris/scale_runners.sh --install-cron
```

Cron fires it every minute: an idle check costs ~2 seconds and logs nothing;
when jobs stack up it becomes the supervisor until the backlog drains (a
flock guard keeps it single-instance, so overlapping fires are no-ops).
Activity logs to `scale-runners.log` in the deploy dir. It can also be run
by hand (`scale_runners.sh [N]`) — same behavior, plus console output.

Safe by design: job claiming is atomic (`FOR UPDATE SKIP LOCKED`) and
per-session serialized, so workers never collide with the main runner or
each other, and a killed worker's job is re-queued by the reaper when its
lease expires. The default cap is 2 extra workers (set
`TWAIN_MAX_EXTRA_RUNNERS` in the deploy dir's `.env` to change it); keep it
small — the login node has a ~6 GB/user memory cap.

### Auto-update from master (cron)

GitHub's hosted Actions runners can't reach RIS (campus network only), so the
cluster updates itself by *pulling*: a cron job polls `origin/master` every 10
minutes and, when it moves, resets the deploy dir to it, refreshes the pixi
env, and restarts the runner tmux session. The repo is public, so no
credentials are needed. Updates are **deferred while a run is in flight**
(any `claimed`/`running` row in the jobs table, or a Slurm job in the queue)
and retried on the next cycle, so a run is never interrupted.

One-time install on the login node:

```bash
bash /storage2/fs1/mdan/Active/dtrc2026-workshop/twain-backend/scripts/ris/auto_update.sh --install-cron
```

On its first real run the script converts the rsync-deployed dir into a git
clone in place (`.env` and logs are untracked and survive). Activity is logged
to `twain-backend/auto-update.log`; remove the crontab line (`crontab -e`) to
turn it off. Note the deploy then tracks **master only** — feature-branch
testing on RIS still goes through `deploy.sh`, which will be overwritten at
the next master merge.

Notes and limits:
- The runner itself is light (DB polling + LLM calls) and fits the login
  node's 6 GB/user cap; all real computation goes to compute nodes via Slurm.
- Jobs whose compute target is **local** would execute on the login node —
  fine for light library runs, but heavy calculators should use the Slurm
  target (the default here).
- If the AWS ECS runner (`twain-runner` service) is also running, both
  runners compete to claim jobs — whichever claims first wins. Scale the ECS
  service to 0 if RIS should handle everything.

### Which account should the runner run under?

Today it runs under a personal WUSTL account, which is fine as a proof of
concept but wrong long-term:

- **Account lifecycle** — when that person graduates or their credentials
  expire, the runner dies, and nobody else can read the chmod-600 `.env` or
  restart their tmux session.
- **Attribution** — every Slurm job from every teammate's UI run is submitted
  as that one user; RIS admins investigating a misbehaving job come to them.
- **Single point of restart** — only the account owner can redeploy, restart,
  or rotate the DB password.

Preferred fix, in order:

1. **RIS service/lab account.** Ask the PI who owns the `compute2-mdan`
   allocation to request a project-level account from RIS. Migration is just
   re-running `deploy.sh` as that user (the code doesn't care whose account
   it is) and moving the `.env` secrets.
2. **Per-member runner instances.** Until then, any team member can run their
   *own* runner: copy `scripts/ris/env.ris.example` to `.env.ris`, fill in
   the LLM creds + RDS password, and run `deploy.sh` under their account.
   Multiple runners are safe — they share the jobs queue and claiming is
   atomic, so each job runs exactly once. This also removes the
   one-person-restart problem.

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

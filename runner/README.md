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
texts their `phone` (migration `005_user_contact.sql`). See
`runner/notifications.py`. `TWAIN_APP_URL` adds a deep link back to the run.

**The owner is the only destination.** There is no global fallback inbox or SNS
topic: a run whose owner has no address (or no phone) on file is not notified at
all, logged at WARNING naming the run. Redirecting elsewhere hands one
researcher's prompt, results and gate questions to somebody who can neither act
on them nor switch them off — notification preferences live on the owner's row —
so the remedy for a missing address is to populate `users.email`, not to reroute
the mail. Note `users.email` is nullable and an Entra token with no
`preferred_username`/`email`/`upn` claim stores an empty one, so this case is
real; the WARNING is how you find those accounts.

Every send is de-duplicated and rate-capped **per run** before it reaches a
backend (`TWAIN_NOTIFY_DEDUPE_SECONDS`, default 120s; `TWAIN_NOTIFY_MAX_PER_HOUR`,
default 12 — either `0` disables that rail). Notifications are best-effort and
never fail a run, so without these rails anything that drives one run repeatedly
turns into one email per drive. A dropped notification is logged with the reason.
Sends and failures are logged too (`twain.runner.notify`, INFO for an accepted
send, ERROR with SendGrid's own message for a rejection), so the runner log
answers "was the researcher actually told?" — set `TWAIN_LOG_LEVEL` to change the
level.

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
(Story 5.4). Set `TWAIN_EXECUTE_SLURM=1` to route every run there (this is how
the RIS deployment runs — the web app no longer offers a per-run choice), or
pass `--slurm` to the orchestrator CLI:

```bash
pixi run python modules/07_runtime_orchestrator/orchestrator.py --slurm
```

**Job control** (submit/poll/cancel/accounting/logs) goes through one of two
backends, chosen by `TWAIN_SLURM_BACKEND`:
- **`api`** (default) — the [RIS API](https://d3n2m687w2hvtj.cloudfront.net/api/redoc),
  a hosted HTTPS gateway onto the same Slurm scheduler. Requires `RIS_API_TOKEN`
  (a bearer PAT; in AWS it is the Terraform-managed `TWAIN/ris_api/TOKEN` -- see
  the one-time prerequisites below -- or set directly in `.env`/`.env.ris`
  locally). Generate it in the RIS API web app; it expires after a week, a
  month, or never (your choice at creation), so pick deliberately and rotate it
  in `terraform/secrets.json` + `terraform apply` + a runner redeploy. ris-api
  rate-limits per token (60 requests/min): each running job polls twice a
  minute at the default 30 s interval, so ~25 concurrent jobs on one PAT is the
  ceiling before polls start failing with 429 (tolerated as lost contact). `RIS_API_BASE_URL` overrides
  the endpoint if it ever changes.
- **`ssh`** — the original `sbatch`/`squeue`/`sacct`/`scancel` path over SSH,
  kept as a fallback (`SlurmAdapter`, `modules/08_execution_adapter/slurm_adapter.py`).

Either way, **staging and the pre-submit preflight probe still go over SSH** —
the RIS API has no file-transfer endpoints (job submission takes only an
inline script, ≤64KB) and no way to run an arbitrary command on the login
node, so those two things can't move to it. Only the scheduler control-plane
calls (what used to be `sbatch`/`squeue`/`sacct`/`scancel` over SSH) go over
HTTPS instead.

What happens at EXECUTE:
1. **stage** — the bundle is rsynced to
   `<storage_root>/twain-runs/<session_id>/` on the cluster
   (`configs/clusters/compute2.json` points at the writable allocation dir);
2. **submit** — on `api`, a `JobSubmitSpec` is POSTed directly (partition
   auto-selected from CPU/GPU/wall-time; the script embeds the same
   `module load ris slurm` + payload the SSH path renders into `#SBATCH`
   lines); on `ssh`, an `#SBATCH` script is rendered and submitted via
   `sbatch` on a login node over SSH;
3. **wait** — the job is polled (bounded): `GET /jobs/{id}` on `api`,
   `squeue`/`sacct` on `ssh`. If the wait budget expires the job is **left
   running** and the result says how to check on it — a multi-hour job is
   never killed just because our wait was shorter;
4. **fetch** — outputs (`results.csv`) are rsynced back into the session's
   artifacts dir either way; stdout, stderr (the last 64 KB, where tracebacks
   land) and accounting (Elapsed/MaxRSS) come from `GET /jobs/{id}/stdout`,
   `/output/stderr?tail=` and `/accounting` on `api`, or the rsynced job log
   (both streams in one file) + `sacct` on `ssh`.

**Live activity in the chat UI.** Between "Plan approved" and the result, each
stage publishes `stage.progress` run events (BUILD: script written; REPAIR:
smoke test, fix rounds, review; EXECUTE: bundle staged → env check → submitted
with job id and resources → queued with Slurm's reason in plain words →
running on its node → finished → results fetched). On the API backend, the
running job's stdout is followed through ris-api's paged output endpoint and
published as `job.log` events (at most 8 KB per poll and 256 KB per job). The
chat screen polls `GET /api/conversations/{id}/activity?after=<id>` over the
authenticated client (the SSE stream can't send the bearer token) and renders
them as a live checklist plus a job-output tail. Reporting is best effort:
a dropped event only means less detail on screen.

**Webhooks (optional, latency only).** ris-api can POST signed job events
(`job.running`/`completed`/`failed`/`cancelled`/`retrying`) to
`https://d1z5umg4xc2bl8.cloudfront.net/api/ris/webhooks`. The API verifies the
Standard Webhooks signature against `TWAIN/ris_api/WEBHOOK_SECRET`, records the
event in `ris_job_events` (deduped on `webhook-id`), and a trigger NOTIFYs the
runner waiting on that job, which re-polls immediately instead of at its next
30 s tick. Polling stays the source of truth: with no webhook, a bad secret, or
the DB listener down, runs behave exactly as before. ris-api delivers only to
public HTTPS on port 443, so a local API can't receive them.

Job-time secrets (`MP_API_KEY`) never go in the job script: the RIS API stores
every submitted spec and copies it into recipes. They are staged next to the
bundle as a 0600 `.twain_secrets.env` that the job loads and deletes, and the
results pull excludes it. A GPU job whose cluster profile pins `gpu_type` must
use `TWAIN_SLURM_BACKEND=ssh`, since the API takes only a GPU count.

The job builds its own venv from the bundle's `requirements.txt` (compute
nodes have no TWAIN environment), and the smoke test runs first so a missing
dependency fails in seconds instead of after a long queue wait.

**Compiled calculators (GPAW, xtb, DFTB+) can't be pip-installed by the job**
— GPAW needs libxc headers, xtb-python isn't on PyPI at all. For those,
shared environments live under the profile's `envs_root`
(`/storage2/fs1/mdan/Active/common/projects/twain/twain-envs` on compute2); the
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
cd /storage2/fs1/mdan/Active/common/projects/twain/TWAIN
bash scripts/ris/provision_envs.sh           # all specs
bash scripts/ris/provision_envs.sh default   # just one env
```

To add a package (a new plan needs an import the env lacks — the smoke test
names it in the job log, and the pre-submit preflight names it before
queueing), **edit the spec, commit, and rerun the script**. Never
`micromamba install` into a shared env by hand: manual drift is how
`twain-envs/default` silently lost rdkit, and hand edits also race against
teammates' running jobs.

#### Rebuilding a shared env: build beside, verify, promote

Every simulation uses these envs, so a rebuild never touches the live one.
`scripts/ris/rebuild_envs.sh` builds a new **version** beside it, verifies it,
and then re-points the env's name at it:

```
$TWAIN_ENVS_ROOT/
  .versions/<version>/<env>        real conda prefixes (built at their final path)
  <env> -> .versions/<version>/<env>   what jobs run ($TWAIN_ENVS_ROOT/<env>/bin/python)
  .retired-<stamp>/<env>           what <env> was before the promote
```

```bash
. /storage2/fs1/mdan/Active/common/projects/twain/TWAIN/twain.sh
S="$CODE_DIR/scripts/ris/rebuild_envs.sh"
bash "$S" build   2026-10-07 nwchem   # one env per Slurm job on general-short (30 min cap)
bash "$S" verify  2026-10-07 nwchem   # imports, engine binary, no foreign prefixes, ACLs
bash "$S" promote 2026-10-07 nwchem   # verify again, then nwchem -> .versions/2026-10-07/nwchem
bash "$S" rollback nwchem 2026-09-01  # re-point at an older version
bash "$S" status
```

- **Versions are built at their final path.** Conda environments hard-code
  their location: shebangs, `activate.d` hooks, `conda-meta`. A copied or
  moved env keeps running code from wherever it came from. That's why
  versions live under `.versions/` and only the symlink moves. **Never `cp`
  an env into place.** The 2026-10-07 copy from the old `dtrc2026-workshop`
  tree left 31–68 files per env pointing back at it.
- **Verification** checks that the expected imports and engine binary
  work, that no file in `bin/` or `etc/` names another env's prefix, that
  `conda-meta/history` starts with the version's own build, and the ACLs.
- **storage2 is NFSv4: mode bits lie and `umask` is ignored.** New files
  show as `rwxrwxrwx`, and what decides access is the ACL (`nfs4_getfacl`).
  The script runs `chmod -R go-w` on each build and fails verification if
  any entry lets `EVERYONE@` or `domain users` (gid 1000070) write. The
  mdan lab's storage groups (`storage2-mdan-common-rw`, `storage2-mdan-rw`)
  keep write access through inheritance; that's the trust boundary.
- **Running it through the RIS API, as TWAIN did on 2026-10-07:** submit one
  job per env (`general-short`, `compute2-mdan`, 4 CPUs, 16 GB, 30 min)
  whose script sources `twain.sh` and runs `rebuild_envs.sh build <version>
  <env>`, followed by one short job for `promote`. Builds take 4–16 minutes
  each. The package cache (`$TWAIN_HOME/.micromamba`, about 6.5 GB) makes
  later builds faster.
- A shared-env change is an **approved** change: edit the spec, get
  sign-off from `TWAIN_ENV_APPROVERS`, commit, and then rebuild. #187
  automates the proposal and approval.

**The specs also gate planning.** Under `TWAIN_EXECUTE_SLURM`, discovery
only plans around a library whose packages the cluster can actually get:
declared by a spec in `scripts/ris/envs/`, or genuinely installable from
PyPI (checked live, cached; known unbuildable-on-nodes packages like gpaw
and the conda-only codes are also blockable offline via
`CONDA_ONLY_PACKAGES` in `dependency_inferencer.py`). Anything else is
rerouted to a runnable tool at plan time instead of dying in the job's
`pip install`. The reroute is never silent: the plan carries an
"ENGINE UNAVAILABLE ON THIS DEPLOYMENT" safety note naming the passed-over
engine and the substitute, and the approval card turns it into a one-tap,
prefilled GitHub issue asking the team to provision the engine — the
researcher decides whether to run the substitute or request the real thing.
So to make a conda-only tool (e.g. Psi4) available on RIS:
add its spec, provision it, commit — planning picks it up from the spec
alone. If a run still fails with a Python traceback inside the generated
script, EXECUTE feeds that traceback back to the repair LLM and resubmits
automatically (bounded by `TWAIN_RUNTIME_REPAIR_ATTEMPTS`, default 2).

To verify an env exactly the way the Slurm job invokes it (no activation;
`OPAL_PREFIX` tells OpenMPI where its runtime data lives — always use the
ABSOLUTE path, a relative mpirun path breaks OpenMPI's prefix
auto-detection):

```bash
ROOT=/storage2/fs1/mdan/Active/common/projects/twain
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

### Event-driven worker (P2) -- `twain-runner` on ECS, no process waits on a job

`runner/worker.py` replaces the always-on polling runner. It is stateless and
scales horizontally; RDS and S3 hold everything a run needs.

* **Dispatch.** With `TWAIN_DISPATCH=sqs` the API inserts each job as
  `dispatching` (a transactional outbox: the row is the event) and, after the
  commit, sends its id to the SQS FIFO queue (`MessageGroupId` = run, so one
  run's jobs stay ordered while runs proceed in parallel). A failed send is
  re-sent by the relay. The login-node runner claims only `queued` rows, so it
  never sees these jobs.
* **Workers** long-poll the queue, claim the job by id (a duplicate message is a
  no-op), and drive it with the same `process_job` as before, keeping the
  message invisible and the job's lease fresh while it runs. Retries and
  reaped jobs go back to `dispatching` -- decided by the job row, whichever
  runner reaps it.
* **Detached EXECUTE.** EXECUTE submits through the RIS API (with S3 staging),
  records the Slurm job in `cluster_jobs`, and **pauses the run** -- the same
  pause an approval uses. Nothing holds a process for the job's life.
* **The cluster monitor** (one active across all workers, via a Postgres
  advisory lock) polls every open cluster job -- woken at once by ris-api
  webhooks -- publishes queue -> running -> finished progress and the stdout
  tail to the chat, and enqueues the run's `resume` when the job finishes. The
  resume collects the outputs from S3, on whichever worker takes it. It also
  relays unsent outbox rows and reaps jobs held by dead workers.
* **Terminate** queues a resume too: a paused run wakes, cancels its Slurm job
  through the RIS API, and settles.

**Activation (once)** -- safe by default; nothing changes until step 5:

1. `terraform -chdir=terraform apply` -- the job queue + dead-letter queue, the
   worker's task role, the API's send permission, the log group, and the
   `twain-runner` ECS service (in the API's subnets/security group).
2. Set the repo variable `TWAIN_ENV_FILE` to the path of your `twain.sh` on RIS
   storage (`gh variable set TWAIN_ENV_FILE --body /storage2/.../twain.sh`), and
   make sure `$CODE_DIR` in it is a current checkout (`git -C "$CODE_DIR" pull`).
3. Merge -- `ci-runner.yml` builds the worker image (without the `sim` stack:
   calculations and smoke tests run on RIS) and deploys it to the service.
4. Check the worker's log (`/ecs/twain-runner`): `worker up: 2 consumers` and
   `[monitor] leading: watching cluster jobs`.
5. `gh variable set TWAIN_DISPATCH --body sqs`, then re-run **API Server - Build &
   Deploy** (workflow_dispatch). From then on every new job goes to SQS.
6. Ask RIS to stop the login-node runner (`junbo.y`'s tmux session `twain-runner`
   on c2-login-001); after step 5 it only ever sees `queued` jobs, of which there
   are no new ones.

**Rollback:** `gh variable set TWAIN_DISPATCH --body db` and redeploy the API --
new jobs go back to `queued` for the polling runner. Runs already paused on a
Slurm job still need the worker to resume them.

### S3 staging (`TWAIN_STAGING=s3`) -- no SSH, no VPN, nothing on a login node

With `TWAIN_STAGING=s3` (RIS API backend only), a run's files go through the
run bucket (`terraform output run_bucket_name`) instead of rsync:

1. **submit side** (runner today, the ECS worker in P2) uploads the bundle to
   `runs/<run>/attempt-<n>/input/bundle.tar.gz`, issues a **job ticket** for that
   attempt (random, only its hash is stored, expires after the wait budget plus
   the wall time), and submits a tiny script through the RIS API;
2. **the job** sources `$TWAIN_ENV_FILE` (`twain.sh`, owner-managed, mode 640 --
   template: `scripts/ris/twain.sh.example`), checks that `$CODE_DIR` contains
   the commit that submitted it (exit 4 with `git pull` otherwise), and runs
   `$CODE_DIR/scripts/ris/job_wrapper.sh`: it trades the ticket at
   `POST /api/job-tickets/urls` for presigned URLs (GET `input/`, PUT `output/`
   only), unpacks the bundle in node scratch, picks an env and **smoke-tests it in
   the job** (exit 2 = missing dependency), runs `main.py`, and uploads
   `output/outputs.tar.gz` -- always, so a failed run's logs come back too;
3. the submit side downloads and unpacks the outputs.

No AWS credentials exist on the cluster; the API signs URLs with its task role.
Wrapper exits the failure card explains: **4** stale RIS checkout, **6** could
not fetch the bundle, **7** could not upload the outputs. The SSH path (rsync +
login-node preflight) is unchanged and remains the default until P2.

### Keeping the RIS runner current (interim, until the ECS worker)

The login-node runner updates itself: `scripts/ris/auto_update.sh` runs from
cron every 10 minutes, fetches `master`, and restarts the runner when it is
idle. The repo has been **private** since Aug 2026, so the fetch needs a
credential. Without one it failed 7,186 times in a row, silently, while
production ran Aug 6 code. A failure now writes `auto-update.status` (with when
it began) and one `ERROR` line per run with the fix. One-time setup, **as the
account that owns the runner** (its tmux session, cron, and deploy dir):

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/twain_deploy -C "twain-ris-runner"   # private key stays in ~/.ssh
# GitHub -> washu-dev/TWAIN -> Settings -> Deploy keys -> Add: paste ~/.ssh/twain_deploy.pub, read-only
cd /storage2/fs1/mdan/Active/common/projects/twain/TWAIN
echo "TWAIN_DEPLOY_KEY=$HOME/.ssh/twain_deploy" >> .env                    # .env is untracked
bash scripts/ris/auto_update.sh && git log -1 --format='%h %s'             # now on current master
cat auto-update.status 2>/dev/null || echo "auto-update healthy"
```

If the runner, cron, and deploy dir belong to someone else (today `junbo.y`),
that account must either do the above, or stop its runner (`tmux kill-session
-t twain-runner`, and remove `auto_update.sh` from its crontab) so a new owner
can start one: `tmux new -d -s twain-runner bash scripts/ris/start_runner.sh`,
then `bash scripts/ris/auto_update.sh --install-cron`. Never run two
always-on runners on different code: both claim from the same queue, so runs
would split between them.

Prerequisites and knobs:
- WashU VPN (AnyConnect) + Duo, and an SSH key for the login node
  (`ssh <wustl-key>@c2-login-001.ris.wustl.edu` must work non-interactively) —
  needed either way, for staging + the preflight probe.
- `RIS_API_TOKEN` — bearer PAT for the RIS API; required unless
  `TWAIN_SLURM_BACKEND=ssh`. `RIS_API_BASE_URL` overrides the default endpoint.
- `TWAIN_SLURM_BACKEND` — `api` (default) or `ssh`.
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
bash /storage2/fs1/mdan/Active/common/projects/twain/TWAIN/scripts/ris/start_runner.sh
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
bash /storage2/fs1/mdan/Active/common/projects/twain/TWAIN/scripts/ris/scale_runners.sh --install-cron
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
bash /storage2/fs1/mdan/Active/common/projects/twain/TWAIN/scripts/ris/auto_update.sh --install-cron
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
3. **Secrets** in Terraform (`terraform/secrets.json`, git-ignored; template in
   `secrets.example.json`): the WashU LLM creds under `secure_api/*` and the RIS
   API PAT under `ris_api/TOKEN`. `terraform apply` creates them and grants the
   ECS **execution** role (`ecsTaskExecutionRole`, which resolves the task def's
   `secrets` before the container starts) read on exactly those ARNs plus
   decrypt on the TWAIN KMS key; `terraform output runner_secrets_missing` lists
   any key still to add. Then `scripts/aws/setup_secrets.sh --apply` replaces the
   task def's `…-REPLACE` ARNs from `terraform output runner_secret_arns`.
   The DB password is already the shared `AWS_SECRET_ARN`.
4. Repo secrets `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (same as the API).
5. Task role: RDS access. (Secret injection uses the execution role -- step 3.)

Execution mode (env in the task def):
- **`TWAIN_EXECUTE_LOCALLY=1`** (the default set here) — run calculations for real,
  keeping the web-UI plan-approval step. This is what computes the GPAW band gap.
- **`TWAIN_AUTO_RUN=1`** — fully unattended: also skip the plan-approval and
  heavy-calc gates. Swap it in if you want hands-off runs with no approval click.

The image is identical locally and on ECS, so a run behaves the same in Docker and
in Fargate. EXECUTE runs generated code, so keep the task sandboxed (resource
limits, minimal IAM/network). To sanity-check the build before pushing, see
"Run in Docker (any OS)" above (add `--platform linux/amd64` on Apple Silicon).

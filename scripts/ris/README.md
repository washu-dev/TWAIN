# TWAIN on RIS (Compute2)

What lives on the RIS side, and the scripts that maintain it. TWAIN never logs
in to RIS to run work. The worker submits jobs through the **RIS API**
(`https://d3n2m687w2hvtj.cloudfront.net/api/v1`; token in AWS secret
`TWAIN/ris_api/TOKEN`), and job files move through S3 using presigned URLs.
Diagram [07](../../docs/architecture/07_deployment_dependencies.drawio) shows
how this fits with everything else.

## Layout on storage2 (`$TWAIN_HOME`)

Everything TWAIN owns is under **`/storage2/fs1/mdan/Active/common/projects/twain`**.

```
$TWAIN_HOME/
  TWAIN/                       CODE_DIR: a git checkout of this repo (keep it on master)
    twain.sh -> twain-ris.sh   sourced by every job: TWAIN_HOME, CODE_DIR, TWAIN_ENVS_ROOT, ...
  twain-envs/                  TWAIN_ENVS_ROOT
    <env> -> .versions/<v>/<env>   what jobs run: abinit cp2k default dftbplus gpaw nwchem psi4 qe
    .versions/<v>/<env>            real conda prefixes, never edited in place
  twain-data/                  engine data: slako/ (DFTB+), sssp/ (QE), pseudodojo/ (ABINIT)
  bin/micromamba               the env builder (needs no modules or sudo)
  .micromamba/builds/          per-build package caches (disposable)
```

- **Ignore `/storage2/fs1/mdan/Active/dtrc2026-workshop/`.** It's the old,
  `junbo.y`-owned tree of the retired login-node runner, and nothing references
  it.
- **Write access** to this tree is limited to its owner, the lab's storage
  groups (`storage2-mdan-common-rw`, `storage2-mdan-rw`) and RIS IT. storage2
  is **NFSv4**, so check access with `nfs4_getfacl`: mode bits and `umask` are
  misleading there.

**`twain.sh`** is the deployment's configuration on RIS. The worker points
jobs at it through `TWAIN_ENV_FILE`. **Jobs need `CODE_DIR`** (and
`TWAIN_HOME`); `TWAIN_ENVS_ROOT` is optional and defaults to the cluster
profile's `envs_root`. Keep secrets out of it, because it's world-readable.
Template: [`twain.sh.example`](twain.sh.example).

## What a job does on a compute node

1. Source `twain.sh`. Exit **3** if it's unreadable or `CODE_DIR` isn't a
   checkout.
2. **Stale-checkout guard:** `CODE_DIR` must contain the commit that submitted
   the job (`TWAIN_EXPECTED_SHA`). Otherwise exit **4**, with the
   `git -C $CODE_DIR pull` to run.
3. `$CODE_DIR/scripts/ris/job_wrapper.sh` exchanges the job's ticket at
   `POST /api/job-tickets/urls` for presigned links and downloads
   `input/bundle.tar.gz`. Exit **6** if that fails.
4. `twain_payload.sh` (from the bundle) picks an environment: the first
   candidate under `TWAIN_ENVS_ROOT` that passes the bundle's smoke test, or
   a pip layer on the closest one. It logs `[env] using …`, then runs the smoke
   test and `main.py` under a `timeout`.
5. Upload `output/outputs.tar.gz`. Exit **7** if that fails.

The worker classifies those exit codes into the failure card's headline and
next step.

## Scripts

| Script | Use |
|---|---|
| `job_wrapper.sh` | Runs on the compute node (above). Read from `CODE_DIR`, so pull after merging changes to it |
| `inventory.sh` | **Read-only** inventory of every env (version, Python, conda + pip packages) and `module spider`. The worker submits it daily; planning uses the result (`ris_inventory`) |
| `envs/*.yml` | The environment specs: the source of truth for what each env should contain. Changing one is a shared-environment change and **needs approval** (`TWAIN_ENV_APPROVERS`) |
| `rebuild_envs.sh` | **The way to change an env:** `build <version> <env>` → `verify` → `promote` → (`rollback`). Builds beside the live version, copies instead of hard-linking, uses a fresh cache per build, and verifies imports, binaries, a functional check (e.g. AM1-BCC on ethanol for `nwchem`) and ACLs. See [`runner/README.md`](../../runner/README.md#rebuilding-a-shared-env-build-beside-verify-promote) |
| `provision_envs.sh` | Builds envs straight from the specs (used by `rebuild_envs.sh`; on its own it builds in place) |
| `fetch_data.sh` | Fetches the engine data into `twain-data/`: DFTB+ Slater–Koster sets from GitHub `dftbparams`, SSSP 1.3.0 from Materials Cloud (MD5-checked), and PseudoDojo from GitHub `abinit/pseudo_dojo` at a pinned commit |
| `twain.sh.example` | Template for `twain.sh` |
| `setup.sh`, `start_runner.sh`, `runner_env.sh`, `scale_runners.sh`, `auto_update.sh`, `deploy.sh`, `env.ris.example` | **Legacy:** the retired login-node polling runner. Don't run them alongside the ECS worker |

## Running maintenance jobs

Use the TWAIN token and the RIS API (or a login node). The lab account is
`compute2-mdan`.
- **`general-short`** caps wall time at **30 minutes** but starts immediately.
  Builds take about 5–12 minutes, so use one env per job.
- **`general-cpu`** allows longer jobs but can queue for a day.
- Check a job spec without queueing it: `POST /jobs/preview` (`sbatch
  --test-only`).

```bash
. /storage2/fs1/mdan/Active/common/projects/twain/TWAIN/twain.sh
bash "$CODE_DIR/scripts/ris/rebuild_envs.sh" status
bash "$CODE_DIR/scripts/ris/inventory.sh" | grep '^TWAIN_INVENTORY_JSON' | head -c 300
```

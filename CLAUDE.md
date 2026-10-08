# TWAIN: instructions for Claude Code

TWAIN turns a research request into a planned, approved, executed and validated
computational-chemistry run.
- **Web app:** `app/`, Expo; see `app/CLAUDE.md`.
- **API:** `api/`, FastAPI on ECS `twain-washu`.
- **Worker:** `runner/`, ECS `twain-runner`, with SQS dispatch and the cluster monitor.
- **Pipeline:** `modules/`, a state machine.
- **Calculations:** run on WashU RIS Compute2 through the RIS API, with
  S3-staged job files.

Start from `README.md` and the diagrams `docs/architecture/07_*` and `08_*`;
onboarding is in `docs/DEVELOPER_SETUP.md`.

## Commands
- Everything local: `./dev.sh` (local Postgres in Docker, API on :8000, polling
  runner, app on :3001).
- Pipeline and runner checks: `pixi run -e lint lint`, `pixi run pytest tests/unit -q`,
  `pixi run pytest runner/tests -q`.
- API: `cd api && pixi run --manifest-path ../pixi.toml python -m pytest -q`.
- App: `cd app && npx tsc --noEmit && npx expo lint`.

## Rules (each comes from a real incident)
1. **Production is live.** Ask before anything outward-facing or hard to
   reverse: merging, deploying, `terraform apply`, writing to the production
   database (RDS `twaindb`), submitting or cancelling Slurm jobs, changing
   shared RIS environments, changing AWS settings. Approval for one action
   doesn't cover the next.
2. **Never print secrets.** Pipe them from Secrets Manager (`TWAIN/*`) straight
   into the consuming command.
3. **Verify before concluding.** Check code, logs (`/ecs/twain-runner`,
   `/ecs/twain-api`), the database (read-only) or RIS job output, and report
   failures plainly.
4. **IAM is add-only.** WashU IT denies `iam:DeleteRolePolicy`/`DetachRolePolicy`.
   Never plan a replace or destroy of an IAM policy; add a new resource address
   and `removed { lifecycle { destroy = false } }` the old one.
5. **ECS task secrets use full ARNs.** A bare name in `valueFrom` is read as an
   SSM parameter, and every task fails to start.
6. **Shared RIS environments are never edited in place, or `cp`'d.** Change
   `scripts/ris/envs/*.yml` with approval, then `scripts/ris/rebuild_envs.sh`
   build → verify → promote.
7. **Partitions.** Calculations go to `general-cpu` (`general-gpu` for GPU
   work). `general-short` (30-minute limit) is only for smoke and maintenance
   jobs. Account `compute2-mdan`.
8. **On RIS, use `$TWAIN_HOME` only:** `/storage2/fs1/mdan/Active/common/projects/twain`.
   Ignore `dtrc2026-workshop`. storage2 is NFSv4: check ACLs with
   `nfs4_getfacl`, not mode bits.
9. **API routes are sync `def`** (or `await run_in_threadpool`);
   `api/test_event_loop.py` enforces this.
10. **Migrations** (`api/migrations/`) are idempotent and applied at API
    startup. Ship them with or before the code that needs them.
11. **Never write `[skip ci]`** (or similar markers) in commit messages or PR
    bodies; a squash merge skips the deploy. Branch from `origin/master`; keep
    PRs small and tested.
12. **A result is only as good as its check.** Don't call a run successful
    because Slurm said COMPLETED; read VALIDATE's verdict and whether anything
    verified the value.

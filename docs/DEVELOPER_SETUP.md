# TWAIN developer setup: from a fresh laptop to a running app

This guide takes a new developer from an empty machine to:
- the whole TWAIN stack running locally: Postgres, the API (FastAPI, `api/`),
  the runner/pipeline (`runner/`, `modules/`) and the web app (Expo, `app/`);
- the same tests and checks CI runs;
- the production database open in DBeaver, **read-only**;
- Claude Code set up the way the team uses it: plugins, MCP servers, project
  rules and memory.

It's written for macOS. Windows equivalents are given where they differ. Budget
half a day, mostly waiting for account access (§1).

> **Read this first.**
> - **There is no shared dev environment in AWS.** The production database
>   (`twaindb` on RDS), the worker and the RIS environments are all live.
>   Locally you get your own Postgres in Docker, and that's where you develop.
> - Open the production database only through a **read-only** connection (§10).
> - Anything that changes AWS, RIS or production data is a reviewed,
>   deliberate step, never a side effect of local development.

For what the pieces are, see the [root README](../README.md) and the two
diagrams in [`docs/architecture/`](architecture/DIAGRAMS_INDEX.md)
(07: deployment and dependencies; 08: one run end to end).

---

## Contents
1. [Accounts and access to request](#1-accounts-and-access-to-request)
2. [Base tools](#2-base-tools)
3. [Git, GitHub and the repository](#3-git-github-and-the-repository)
4. [AWS command line](#4-aws-command-line)
5. [Python: pixi environments](#5-python-pixi-environments)
6. [Node and the web app](#6-node-and-the-web-app)
7. [Local configuration (`.env` files)](#7-local-configuration-env-files)
8. [Run the stack](#8-run-the-stack)
9. [Tests and checks (what CI runs)](#9-tests-and-checks-what-ci-runs)
10. [DBeaver (local and read-only production)](#10-dbeaver-local-and-read-only-production)
11. [VS Code](#11-vs-code)
12. [Claude Code](#12-claude-code)
13. [Optional: RIS (Compute2) and the RIS API](#13-optional-ris-compute2-and-the-ris-api)
14. [Day-one checklist](#14-day-one-checklist)
15. [Troubleshooting](#15-troubleshooting)
16. [Where to read next](#16-where-to-read-next)

---

## 1. Accounts and access to request

Ask your team lead on day one; several of these take time.

| Access | What it's for | How |
|---|---|---|
| **WashU key + Microsoft Entra sign-in** | Signing in to the web app | You have it as WashU staff or student |
| **WashU VPN** | The LLM gateway (`aiapi.wustl.edu`, which the pipeline uses to call Claude) only answers on campus or over VPN | WashU IT |
| **LLM gateway client credentials** (`API_KEY`, `CLIENT_ID`, `CLIENT_SECRET`) | Running the pipeline locally | From your lead, or read from `TWAIN/secure_api/*` once your AWS user can (§4) |
| **GitHub**, member of the `washu-dev` org | The repo `washu-dev/TWAIN`, PRs, the [project board](https://github.com/orgs/washu-dev/projects/13) | Your lead invites your GitHub account |
| **AWS IAM user** (account `730335203321`, region `us-east-1`) | `aws` commands; reading the `TWAIN/*` secrets; CloudWatch logs (`/ecs/twain-api`, `/ecs/twain-runner`) | Your lead creates it with read access to `TWAIN/*` (or lets you assume `TWAIN-secrets-reader`). **WashU policy forbids deleting IAM policies**, so ask for exactly what you need |
| **Read-only database login** | DBeaver on production `twaindb` | As of 2026-10 only the master user exists. An admin creates the read-only group once, then one login per developer (§10). Never use `postgres` for day-to-day work |
| **Claude** | Claude Code in the terminal and VS Code | Your lead adds you to the team's Claude plan |
| *Optional:* **RIS Compute2 account** (`compute2-mdan`) + **ris-api personal access token** | Running or inspecting Slurm jobs, the ris-api Claude plugin, RIS maintenance | §13 |

---

## 2. Base tools

### macOS

```bash
xcode-select --install                                   # Apple command-line tools (git, compilers)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# Apple Silicon: run the two lines the installer prints to add brew to your PATH, then open a new terminal.

brew install git gh jq awscli postgresql@18             # postgresql@18 = the psql client (matches RDS 18.4)
brew install --cask visual-studio-code dbeaver-community drawio
brew install docker colima                               # local Postgres runs in Docker; Colima is the free engine
# (Docker Desktop works too: brew install --cask docker)
brew install terraform                                   # only if you'll touch terraform/ (needs >= 1.7)
```

**pixi** manages every Python environment (§5):

```bash
curl -fsSL https://pixi.sh/install.sh | sh               # then open a new terminal
pixi --version
```

**Node 20** (what CI uses), via nvm so versions are easy to switch:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
# open a new terminal, then:
nvm install 20 && nvm alias default 20
node -v   # v20.x
```

### Windows

Develop inside **WSL2 (Ubuntu)**. `dev.sh`, the RIS scripts and pixi's
calculator environments assume Linux or macOS. In PowerShell (as Administrator):

```powershell
wsl --install -d Ubuntu                                  # reboot, then create your Linux user
winget install Microsoft.VisualStudioCode DBeaver.DBeaver.Community Docker.DockerDesktop JGraph.Draw
```

Then, inside Ubuntu: `sudo apt install -y git jq postgresql-client awscli`, and
follow the macOS steps for `gh` (https://cli.github.com), pixi and nvm. In
Docker Desktop, enable *Settings → Resources → WSL integration* for Ubuntu. In
VS Code, install the **WSL** extension and open the repo with `code .` from
inside WSL.

### Versions this project expects

| Tool | Version | Why |
|---|---|---|
| Python | **3.12** (pixi `default`), 3.11 (pixi `sim`) | The API image is `python:3.12-slim`; CI runs 3.12 |
| Node | **20** | The app's CI job |
| PostgreSQL | **18** client; 16 for the local container | RDS runs 18.4; `dev.sh` starts `postgres:16` |
| Terraform | ≥ 1.7 (1.11 in use) | `terraform/versions.tf` (`removed` blocks) |
| Docker | any recent | the local database |

---

## 3. Git, GitHub and the repository

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@wustl.edu"
gh auth login                       # GitHub.com → HTTPS → log in with a browser
mkdir -p ~/projects && cd ~/projects
gh repo clone washu-dev/TWAIN
cd TWAIN
```

> Clone to a path **without spaces** if you can. Several scripts quote paths
> correctly, but tools you add later may not. Also keep the folder above the
> repository free of other projects' `CLAUDE.md` files: Claude Code reads every
> `CLAUDE.md` from the repo up to your home directory (§12).

### How we work
- **Branches.** Branch from **`origin/master`** for every change, named
  `feat/…`, `fix/…`, `docs/…`, `chore/…` or `infra/…`.
- **Pull requests.** CI must pass before merging.
- **Merging to `master` deploys.** Each workflow is path-filtered:

  | Change under | Redeploys |
  |---|---|
  | `api/**` | the API (ECS `twain-washu`) |
  | `runner/`, `modules/`, `configs/`, `schemas/`, `pixi.*` | the worker (ECS `twain-runner`) |
  | `app/**` | the web app (S3 + CloudFront) |

  The API and the app each carry their own version (`api/VERSION`,
  `app/VERSION`, as `YYYY.MM.DD.NNN`), bumped by CI. Never edit them by hand.
- **Never commit** `.env` files, credentials, `terraform/secrets.json`,
  `*.tfstate` or data files. `.gitignore` covers the usual ones.
- **Migrations** (`api/migrations/NNN_*.sql`) must be idempotent. The API
  applies them at startup, so a migration ships with (or before) the code that
  needs it.
- **Commit messages and PR bodies must not contain the literal text
  `[skip ci]`** (or similar markers). GitHub honours it even in prose, and a
  squash merge carries it onto `master`, silently skipping the deploy. Only the
  bot's release commits use it.

---

## 4. AWS command line

Once your IAM user exists, your lead gives you an access key:

```bash
aws configure
#   AWS Access Key ID:     <from your lead>
#   AWS Secret Access Key: <from your lead>
#   Default region name:   us-east-1
#   Default output format: json
aws sts get-caller-identity          # should show account 730335203321 and your user
```

Things you'll use (all read-only):

```bash
aws logs tail /ecs/twain-runner --since 30m --follow     # the worker (runs, monitor, inventory)
aws logs tail /ecs/twain-api --since 30m                 # the API
aws ecs describe-services --cluster twain-cluster --services twain-runner twain-washu \
  --query 'services[].{svc:serviceName,running:runningCount,events:events[0].message}'
```

To read a secret without printing it, **pipe it straight into the command that
needs it**:
`PGPASSWORD=$(aws secretsmanager get-secret-value --secret-id … --query SecretString --output text) psql …`.
Never paste a secret into chat, an issue or a commit.

---

## 5. Python: pixi environments

All Python (the API, runner, pipeline, tests and linters) runs in **pixi**
environments defined in `pixi.toml`. There is no `venv` to create by hand.

```bash
pixi install              # the default env: Python 3.12 + API/runner/pipeline deps + the chemistry toolset
pixi run python -V        # 3.12.x
pixi install -e lint      # ruff only (what CI lints with)
pixi install -e sim       # OPTIONAL, large: calculator stack (Python 3.11) for running bundles locally
```

| Env | For |
|---|---|
| `default` | API, runner, pipeline, tests. Includes rdkit, ase, pymatgen, psi4, xtb-python, openmm, openff-toolkit and more |
| `lint` | `ruff` |
| `sim` | Running generated calculations locally (dftbplus, nwchem, gpaw on Linux, matgl, …). Kept in step with the RIS env specs (`scripts/ris/envs/*.yml`). You rarely need it: production runs calculations on RIS |

Run anything inside an environment with `pixi run <cmd>` (or `pixi run -e sim <cmd>`),
or open a shell in it with `pixi shell`.

DFTB+ runs locally need the Slater-Koster parameter files:
`pixi run fetch-slako` (fetched, never committed).

---

## 6. Node and the web app

```bash
cd app
npm ci                     # exact versions from package-lock.json, as CI installs them
cp .env.example .env       # then see §7
cd ..
```

> The app uses **Expo SDK 56**. Expo changes quickly: read the versioned docs
> (https://docs.expo.dev/versions/v56.0.0/) before changing app code
> (`app/AGENTS.md` says the same to Claude).

---

## 7. Local configuration (`.env` files)

**Repo root `.env`** (read by `dev.sh`, the runner and the pipeline):

```bash
cp .env.example .env
```

Fill in only these to start:

| Variable | Value |
|---|---|
| `API_KEY`, `CLIENT_ID`, `CLIENT_SECRET` | LLM gateway credentials (§1). Required for any real run |

`.env.example` documents every other variable. The defaults suit local work:
`dev.sh` points the API and runner at your local Postgres and turns auth off.
**Don't** put production database credentials in `.env`.

**`app/.env`**:

| Variable | Local value |
|---|---|
| `EXPO_PUBLIC_API_BASE_URL` | `http://localhost:8000` |
| `EXPO_PUBLIC_AUTH_DISABLED` | `true` (pairs with the API's `AUTH_DISABLED=true` that `dev.sh` sets), or leave sign-in on and set the Entra ids from your lead |

---

## 8. Run the stack

Connect the **VPN** (the pipeline calls the LLM gateway), then:

```bash
./dev.sh
```

It starts, in order:
1. **Postgres:** a native server if `psql` can reach one, otherwise the
   `twain-pg` container (`postgres:16` on :5432, created on first run, with
   Colima started if needed). It applies every migration in `api/migrations/`.
2. **The API** on http://localhost:8000 (auth disabled; Swagger at `/docs`).
3. **The runner:** the local *polling* runner (`TWAIN_DISPATCH=db`). By
   default it executes generated scripts locally
   (`TWAIN_EXECUTE_LOCALLY=1`).
4. **The web app** on http://localhost:3001.

Ctrl-C stops everything. Variants:

```bash
./dev.sh --no-execute     # plan only: never runs generated code
./dev.sh --no-app         # backend only
./dev.sh --no-runner      # UI only (chats stop at INTAKE)
```

Open http://localhost:3001, describe a simulation (e.g. *"Estimate the aqueous
solubility of caffeine at 25 °C as logS"*), wait for the plan and approve it.

**Running calculations on RIS from your laptop** (instead of locally): you
need a RIS account and token (§13). Then set `TWAIN_EXECUTE_SLURM=1` and
`RIS_API_TOKEN=…` in `.env`. Production runs this way, through the ECS worker.

---

## 9. Tests and checks (what CI runs)

Run the checks for whatever you touched before opening a PR:

```bash
# pipeline + runner (ci-runner.yml)
pixi run -e lint lint                      # ruff check runner api
pixi run pytest runner/tests -q            # real-Postgres tests run when a local Postgres is up
pixi run pytest tests/unit -q              # ~1,700 pipeline tests

# API (deploy-api.yml)
cd api && pixi run --manifest-path ../pixi.toml python -m pytest -q && cd ..

# web app (deploy-app.yml)
cd app && npx tsc --noEmit && npx expo lint && npm run build && cd ..
```

CI also runs `pip-audit` (API), an npm audit gate and a licence check (app).
`api/test_event_loop.py` fails the build if an API route blocks the event loop:
write routes as plain `def`, or `await run_in_threadpool(...)`.

---

## 10. DBeaver (local and read-only production)

### Your local database
**Database → New Database Connection → PostgreSQL**, with host `localhost`,
port `5432`, database `twaindb`, user `postgres`, password `postgres`. Do
whatever you like here: `./dev.sh` can recreate it.

### Production (read-only)
1. **New Database Connection → PostgreSQL**, and let DBeaver download the driver.
2. **Main tab:**
   - **Host:** `twain-app-database.cn8saqya88cd.us-east-1.rds.amazonaws.com`
   - **Port** `5432`, **Database** `twaindb`
   - **Username / Password:** your **read-only** login (§1). Tick *Save
     password* only if your disk is encrypted (FileVault or BitLocker).
3. **SSL tab:** *Use SSL*, mode **`require`**.
4. **General → Connection type:** choose **Production**, and tick
   **Read-only connection** and **Confirm SQL execution**.
5. If it times out, your network isn't allowed by the database's security
   group. Ask your lead.

Useful tables:

| Table(s) | Holds |
|---|---|
| `conversations` · `messages` | Runs and their chat transcripts |
| `jobs` | Each slice of a run (`dispatching` → `claimed` → `running` → `done`/`error`) |
| `run_events` | Everything the app shows live: `stage.progress` subtasks, `job.log`, `run.error` failure cards |
| `cluster_jobs` | Slurm jobs runs are paused on |
| `ris_inventory` | What the RIS environments actually contain (newest `ingested` row) |
| `users` · `library_availability` · `artifacts` | Users · what the cluster can run · stage outputs |

### For admins: one-time read-only setup, then one login per developer

Run as `postgres`:

```sql
-- once: a read-only group that also covers tables created later by migrations
CREATE ROLE twain_readonly NOLOGIN;
GRANT CONNECT ON DATABASE twaindb TO twain_readonly;
GRANT USAGE ON SCHEMA public TO twain_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO twain_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO twain_readonly;

-- per developer (send the password out of band, never in chat or tickets)
CREATE ROLE ro_<username> LOGIN PASSWORD '<generated>' IN ROLE twain_readonly;
ALTER ROLE ro_<username> SET default_transaction_read_only = on;
ALTER ROLE ro_<username> SET statement_timeout = '5min';
```

---

## 11. VS Code

Open the repo: `code ~/projects/TWAIN`.

**Extensions** (Extensions panel, or `code --install-extension <id>`):

| Extension | ID |
|---|---|
| Python, Pylance | `ms-python.python`, `ms-python.vscode-pylance` |
| Ruff (matches CI) | `charliermarsh.ruff` |
| ESLint | `dbaeumer.vscode-eslint` |
| Expo Tools | `expo.vscode-expo-tools` |
| Claude Code | `anthropic.claude-code` |
| Draw.io (for `docs/architecture/*.drawio`) | `hediet.vscode-drawio` |
| HashiCorp Terraform (infrastructure only) | `hashicorp.terraform` |
| Docker (optional) | `ms-azuretools.vscode-docker` |
| WSL (Windows only) | `ms-vscode-remote.remote-wsl` |

**Interpreter.** Command Palette → *Python: Select Interpreter* →
`./.pixi/envs/default/bin/python`. Tests then run from the Testing panel with
the same packages CI uses.

---

## 12. Claude Code

Claude Code is an AI coding assistant that works in your terminal and in VS
Code, inside this repository. It can read the code, run commands and tests,
inspect AWS and the database (read-only), and open PRs. Much of TWAIN was built
with it.

### Install and sign in

```bash
curl -fsSL https://claude.ai/install.sh | bash      # Windows/WSL: run this inside Ubuntu
# (or, with Node installed: npm install -g @anthropic-ai/claude-code)
claude --version
cd ~/projects/TWAIN && claude                       # first run: sign in with your team Claude account
```

In VS Code, the **Claude Code** extension (§11) adds a panel that uses the
same sign-in. The Claude desktop app is optional.

### Connect the tools the team uses

Run the `/…` commands inside a `claude` session; `claude mcp …` commands go in
your shell.

| What | How | Gives Claude |
|---|---|---|
| **GitHub** | `gh auth login` (§3) covers most work. For the GitHub MCP server: `claude mcp add --transport http github https://api.githubcopilot.com/mcp/ -H "Authorization: Bearer <fine-grained PAT>"`. Keep the PAT out of the repo and out of chat | Issues, PRs, the project board |
| **WashU plugins** | `/plugin marketplace add washu-dev/mcp-plugins`, then `/plugin install ris-api@washu-mcp-plugins` | ris-api tools: your Slurm jobs' status and output, partitions, submissions (needs a RIS account and token, §13) |
| **Official plugins** (optional) | `/plugin install frontend-design@claude-plugins-official` (UI work), `/plugin install claude-code-setup@claude-plugins-official` | Design help, setup helpers |
| **Docs lookup** (optional) | The Context7 connector in your claude.ai account, or `claude mcp add` | Current library docs (Expo, FastAPI, boto3, …) |
| **Team agents** (optional) | Ask your lead about the `di2-saif` plugin marketplace | Architect, test-engineer, config-engineer, … agents |

Check what's connected with `/mcp` and `/plugin`.

Claude also uses the CLIs you've installed: `gh`, `aws`, `psql`, `pixi`,
`terraform`. Configure them with **your own read-mostly credentials** (§1–§4).
Claude acts with your permissions.

### Project instructions and memory
- **`CLAUDE.md` files** hold rules Claude always follows:
  - **`CLAUDE.md`** in the repo root: TWAIN's team rules (the list below,
    condensed);
  - **`app/CLAUDE.md`**: rules for the Expo app;
  - your own `~/.claude/CLAUDE.md`: personal preferences.

  Claude also reads every `CLAUDE.md` in the folders *above* the repo, so keep
  `~/projects/` free of other projects' instructions. Propose team-rule changes
  as a PR.
- **Memory.** Claude keeps per-project memory under
  `~/.claude/projects/<path>/memory/`: facts worth keeping between sessions
  (how RIS is laid out, deploy constraints, decisions). Ask *"what do you
  remember about X?"*, and correct it when it's wrong.

### Working with Claude: the rules this team follows

These come from real incidents on this project. Hold Claude to them, and
yourself too:

1. **Production is live.** Claude must **ask before** anything outward-facing
   or hard to reverse: merging, deploying, `terraform apply`, writing to the
   production database, submitting or cancelling Slurm jobs, changing a shared
   RIS environment, or changing AWS settings. Approval for one action isn't
   approval for the next.
2. **Never print secrets.** Pipe tokens straight into the command that uses
   them, and show only a fingerprint if needed.
3. **Evidence over assumptions.** Ask Claude to check the code, logs, database
   (read-only) or RIS job output before concluding, and to say plainly when
   something failed. It's fine for it to say "I couldn't verify X".
4. **IAM is add-only.** WashU IT denies deleting or detaching IAM role
   policies. Any Terraform plan that **replaces or destroys** an IAM policy
   must be redone (see `terraform/README.md`).
5. **Shared RIS environments are never edited in place.** Change the spec, get
   approval, then use `scripts/ris/rebuild_envs.sh`: build beside, verify,
   promote. Never `cp` a conda environment (§13).
6. **Small PRs from `origin/master`,** with tests, passing the §9 checks.
   Claude writes commit messages and PR descriptions, so review them like code
   (and keep `[skip ci]` out of them).
7. **Read the diff before approving.** You own what gets merged.

Good first prompts:
- "Walk me through what happens when I submit a run (use diagram 08)."
- "Where is the failure card's next step decided?"
- "Run the pipeline unit tests and fix any failures on my branch."
- "Review my diff before I open a PR."
- "Show me the last three runs and how each ended" (read-only database).

---

## 13. Optional: RIS (Compute2) and the RIS API

Only if you'll work on cluster execution or RIS maintenance.

- **Account and token.** Request a RIS Compute2 account on `compute2-mdan`,
  then create a **personal access token** in the ris-api web app, for the
  Claude plugin and for `RIS_API_TOKEN` in your `.env`.
- **Where TWAIN lives on RIS:** `/storage2/fs1/mdan/Active/common/projects/twain`
  (`$TWAIN_HOME`):
  - `TWAIN/` holds the checkout and `twain.sh`;
  - `twain-envs/<env>` points at `.versions/<version>/<env>`;
  - `twain-data/` holds the engine data.

  Ignore anything under `dtrc2026-workshop` (retired). Details:
  [`scripts/ris/README.md`](../scripts/ris/README.md).
- **Partitions.**
  - Calculations run on **`general-cpu`** (or `general-gpu` for GPU plans).
  - **`general-short`** (30-minute limit) is only for smoke tests and
    maintenance jobs.
- **storage2 is NFSv4:** mode bits and `umask` don't show who can write; use
  `nfs4_getfacl`.
- **Changing a shared environment** is an approved change: edit
  `scripts/ris/envs/<env>.yml`, get sign-off (`TWAIN_ENV_APPROVERS`), then
  `rebuild_envs.sh build → verify → promote` (rollback is one command). A run
  that fails for want of a conda-only package proposes the change itself and
  emails the approvers Approve/Reject buttons (#187). See the
  [runner README](../runner/README.md#rebuilding-a-shared-env-build-beside-verify-promote).

---

## 14. Day-one checklist

- [ ] `git`, `gh`, `aws`, `psql`, `pixi`, `node -v` (20), Docker/Colima, VS Code and DBeaver installed
- [ ] `gh repo clone washu-dev/TWAIN` worked
- [ ] `pixi install` finished; `pixi run python -V` prints 3.12
- [ ] `app/`: `npm ci` finished; `.env` and `app/.env` created
- [ ] `./dev.sh` (on VPN): http://localhost:8000/docs and http://localhost:3001 both load, and a test run reaches the plan-approval card
- [ ] The §9 checks pass on `master`
- [ ] `aws sts get-caller-identity` works, and `aws logs tail /ecs/twain-runner --since 1h` shows the worker
- [ ] DBeaver: local `twaindb`, and production **read-only**
- [ ] `claude` starts in the repo; `/mcp` lists GitHub (and ris-api, if you have RIS)
- [ ] You've read the [root README](../README.md) and opened diagrams 07 and 08

---

## 15. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `dev.sh`: Docker not running / cannot connect | `colima start` (or start Docker Desktop), then re-run |
| Port 5432, 8000 or 3001 already in use | Another instance (or a native Postgres) is running. `lsof -i :8000` and stop it, or set `DB_PORT`/`API_PORT` |
| The run never leaves INTAKE, or the LLM calls fail with 403 | Off the WashU network: connect the VPN. Or `.env` is missing `API_KEY`/`CLIENT_ID`/`CLIENT_SECRET` |
| The browser shows nothing / every API call fails | `app/.env` `EXPO_PUBLIC_API_BASE_URL` must be `http://localhost:8000`. Restart `npm run web` after editing `.env` |
| `pixi` can't solve or install | `pixi clean && pixi install`. On Windows, use WSL |
| An API route test fails with an event-loop error | A route does blocking I/O in `async def`. Make it `def`, or wrap the call in `run_in_threadpool` |
| DBeaver or `psql` times out against production | The network isn't in the database's security group. Ask your lead (don't open the security group yourself) |
| A RIS job fails with exit 3 / 6 / 7 | 3: `twain.sh` unreadable. 6: the job couldn't download its bundle (the API's S3 access). 7: output upload. The failure card shows the job's stderr |
| A path with spaces breaks a command | Quote it, or clone to a path without spaces |

---

## 16. Where to read next

- The [root README](../README.md): what TWAIN is and how it's deployed.
- [`docs/architecture/07_deployment_dependencies.drawio`](architecture/07_deployment_dependencies.drawio)
  and [`08_run_lifecycle.drawio`](architecture/08_run_lifecycle.drawio).
- Component READMEs: [app](../app/README.md) · [api](../api/README.md) ·
  [runner](../runner/README.md) · [modules](../modules/README.md) ·
  [scripts/ris](../scripts/ris/README.md) · [terraform](../terraform/README.md).
- The project board: https://github.com/orgs/washu-dev/projects/13

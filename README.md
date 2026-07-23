# TWAIN

TWAIN turns a natural-language research request ("predict the aqueous
solubility of aspirin at 25°C") into a planned, executed, and validated
computational-chemistry run — with a human approval gate before anything is
built or executed.

## Architecture (local dev)

```
Browser (localhost:8081)
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

## Quick start (one command)

```bash
./dev.sh
```

This starts everything: Postgres (a native server if `psql` can reach one,
otherwise the `twain-pg` Docker container — created on first run, with Colima
started automatically on macOS), the API on :8000 (auth disabled for dev), the
runner (with real execution enabled), and the web app on :8081. First run also
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

Then open <http://localhost:8081>, describe a simulation, wait ~30 s for the
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

**Terminal 3 — web app** (port 8081):

```bash
cd app && npm run start
```

## Configuration flags

| Env var | Where | Effect |
|---|---|---|
| `AUTH_DISABLED=true` | API | skip Entra sign-in; every request is a dev admin. Local only. |
| `TWAIN_EXECUTE_LOCALLY=1` | runner | actually run the generated script at EXECUTE (otherwise planning-only) |
| `TWAIN_AUTO_RUN=1` | runner | fully unattended: executes and skips the plan-approval + heavy-calc gates |
| `TWAIN_EXECUTE_SLURM=1` | runner | submit the run to the Compute2 Slurm cluster instead (see `runner/README.md`) |
| `TWAIN_VERIFY_CODEGEN=1` | runner | verify + repair generated scripts before running (defaults on when executing) |
| `DB_HOST/PORT/NAME/USER/PASSWORD` | API + runner | Postgres connection (dev defaults: `localhost:5432`, `twaindb`, `postgres`/`postgres`) |

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

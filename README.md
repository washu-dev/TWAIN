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
| `TWAIN_GITHUB_TOKEN` | pipeline | file `LibraryAddition` install requests as GitHub issues (see below); without it they are ledgered only |
| `TWAIN_GITHUB_REPO` | pipeline | which repo those issues go to (defaults to the git `origin` remote) |
| `TWAIN_LIBRARY_REQUEST_ISSUES` | pipeline | `auto` (default — on iff a token+repo resolve), `1`, or `0` |
| `DB_HOST/PORT/NAME/USER/PASSWORD` | API + runner | Postgres connection (dev defaults: `localhost:5432`, `twaindb`, `postgres`/`postgres`) |

## Missing libraries → `LibraryAddition` requests

TWAIN only ever plans with libraries it can actually import — the preset set in
`pixi.toml`, mirrored by `configs/discovery_registry.json` and
`configs/calculator_registry.json`. That rule is unchanged: a plan never names a
tool the run can't load.

What used to be silent is now tracked. When method discovery reaches for a
library that isn't installed — or the researcher asks for one by name ("compute
it with VASP") — TWAIN:

1. plans with the best **installed** library instead,
2. tells the researcher on the plan they approve (in `safety_notes`, with the
   library it substituted), and
3. records the ask in `logs/library_requests.json` and files a GitHub issue
   tagged **`LibraryAddition`** so the environment can be extended.

Requests are deduplicated by library name, both locally and against open issues
on GitHub, so a library TWAIN keeps wanting is one issue with a rising
`occurrences` count — not one issue per run. With no token configured the request
is still ledgered and still reported; only the issue is skipped, so nothing here
is required to run TWAIN. See
[`modules/04_method_discovery/library_requests.py`](modules/04_method_discovery/library_requests.py).

A library the researcher names that *is* installed is simply honoured: it goes to
the front of the discovery ranking and is offered to the discovery LLM as a
preference, so a direct ask decides the toolset rather than just being noted.

## Reporting an issue from a run

The run window has a **Report** button, live from the moment a run exists until
long after it ends. It opens a short form — category (Bug / Library / Result /
Other), a title, and what happened — and files a GitHub issue with **the run's own
data attached**: the state it reached, the toolset planning chose, the plan notes
the researcher saw, the errors, the transcript tail, and the names of every
artifact produced. A maintainer never has to ask "what were you running?".

Because submitting publishes run data to the issue tracker, the form shows the
exact snapshot that will be attached before you send it, and says up front when a
deployment has no credentials (the report is then saved against the run instead of
filed). Categories map to GitHub labels; `Library` reuses `LibraryAddition`, so a
researcher asking for a missing package lands in the same bucket as discovery's
own requests. Endpoints, statuses, and deployment config are in
[`api/README.md`](api/README.md).

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

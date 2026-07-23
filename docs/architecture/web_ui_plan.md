# TWAIN Web UI — Architecture & Delivery Plan

**Status:** Approved plan, Phase 0 in progress
**Date:** 2026-07-07
**Branch:** `UserInterface`
**Confirmed decisions:**
- **Authentication:** Microsoft Entra ID (Azure AD) OIDC — WashU's SSO, the same tenant the engine already uses for the LLM gateway.
- **Authorization / admin:** local role table in Postgres (`users.role`); existing admins promote/demote others in-app.
- **Deployment:** AWS (the older docs' mention of GCP Cloud Run is stale — AWS ECS/S3/CloudFront is the source of truth).

---

## 1. Goals

1. Sign in to a website on the web (WashU SSO).
2. Designate certain users as admins.
3. Query the TWAIN agent through a chat interface.
4. Keep track of previous conversations and view old reports.
5. Re-run a previous conversation from any point in the state machine.

---

## 2. What is already in place

### 2.1 AWS (account `730335203321`, region `us-east-1`) — provisioned *and* auto-deploying
| Concern | Resource |
|---|---|
| Frontend hosting | S3 `twain-dev-1781888831` → CloudFront `E3PINHJ1G0F5PS` (`d1z5umg4xc2bl8.cloudfront.net`) |
| API hosting | ECR `twain-ecr` → ECS Fargate cluster `twain-cluster`, service `twain-api` (512 CPU / 1 GB) |
| Database | RDS PostgreSQL `twain-app-database.cn8saqya88cd.us-east-1.rds.amazonaws.com`, db `twaindb` |
| Secrets | Secrets Manager `DBPASSWORD-xfmLoq` (DB password, resolved at runtime via boto3) |
| Logs | CloudWatch `/ecs/twain-api` |
| CI/CD | `.github/workflows/deploy-app.yml` (Expo web → S3 → CloudFront invalidation) and `deploy-api.yml` (Docker → ECR → ECS) — both run on push to `master`, path-filtered, with lint + tests + audit gates |
| Planned API domain | `https://twain-api.wustl.edu` |

### 2.2 Frontend — `app/` (Expo v56 / React Native Web)
- Expo Router, React 19, axios, TypeScript; WashU-branded theme (`src/constants/theme.ts`).
- Single screen (`src/screens/HomeScreen.tsx`) with a Header (Login button = **stub**, Test button), three tiles that already name the goals — **Start Simulation** (chat), **Resume Workflow** (rerun), **Browse** (history/reports) — a Footer, and a MessageModal.
- API client singleton (`src/api/client.ts`) reads `EXPO_PUBLIC_API_BASE_URL`.

### 2.3 API — `api/` (FastAPI on ECS)
- `/api/health` + a `/api/greetings` demo only. psycopg2 + Secrets Manager. CORS already allows the CloudFront origin. **Not yet connected to the engine.**

### 2.4 The engine — the pipeline (fully built)
- **State machine** (`modules/16_agent_mesh_control_plane/statemachine.py`): 13 states — INTAKE → CLARIFY → DECOMPOSE → DISCOVER → PLAN → BUILD → EXECUTE → INTERPRET → VALIDATE → {ACCEPT │ CORRECT→BUILD │ REPLAN→PLAN} → TERMINATE — with a guard table over a `Context` dataclass.
- **Orchestrator** (`modules/07_runtime_orchestrator/orchestrator.py`): `Orchestrator(session_id, researcher_id, request=, agent=, ask=, store=, ...).run(until=State)` drives the machine with retries, a circuit breaker, a budget tracker, per-stage timeouts, and a checkpoint after every stage.
- **Persistence:** `RunSession` + a SQLite `Store` (`modules/14_provenance_memory/store.py`) exposing `list_sessions(researcher_id)`, `get_session`, `resume_session`, `save_session`; a hash-chained provenance event log per run; `ResultPackage` (`modules/10_result_interpreter/`) = the "report."
- **LLM access** (`modules/16_agent_mesh_control_plane/AgentInterface.py`): WashU gateway `https://aiapi.wustl.edu/models/v2/messages` (default `claude-opus-4-8`), authenticated via **Entra ID** client-credentials against WashU tenant `4ccca3b5-71cd-4e6d-974b-4d9beb96c6d6`.
- **Path wiring** (`modules/07_runtime_orchestrator/_bootstrap.py`): the numbered `modules/*` dirs are put on `sys.path` so the orchestrator runs standalone under `pixi run python`.

### 2.5 Hooks that already anticipate the five goals
- `researcher_id` is a first-class field on every `RunSession` and `Store` query → **multi-user is already modeled**.
- `run(until=STATE)` → pause/stop at any state = the primitive for **"rerun from any point."**
- Resume-from-checkpoint + deterministic provenance replay already exist.
- The orchestrator's **`ask` callable** is the human-in-the-loop seam that **chat clarification and plan-approval** plug into (module 13 is otherwise an empty stub).

### 2.6 What the original docs already specified for the web UI
- Backlog stretch story **S1 "Web UI for Approval & Result Review"** — FastAPI + React, visual plan with approve/reject/edit, results with Plotly (`docs/backlog/DETAILED_BACKLOG.md`).
- Roadmap **Phase 8 "Production Hardening"** — multi-tenant OAuth auth + a cost-monitoring dashboard (`../project/IMPLEMENTATION_ROADMAP.md`).
- **`../project/REVIEW.md`** — CLI first, web UI later; approval UI shows plan + cost and waits for yes/no/edit.

---

## 3. Gaps to close (per goal)

| Goal | Gap |
|---|---|
| 1. Sign in | No auth backend; Login is a UI stub. `researcher_id` exists but nothing populates/verifies it. |
| 2. Admin | No role/RBAC concept anywhere. |
| 3. Chat | Intake is single-shot; no conversation model, no streaming, HITL module is a stub. |
| 4. History + reports | Data exists (`list_sessions`, `ResultPackage`) but no API/UI exposes it. |
| 5. Rerun from a state | Primitives exist (`run(until=)`, resume, provenance) but no fork API/UI. |

---

## 4. Target architecture

```
                 Browser / mobile (Expo RN Web)
                          │  (HTTPS, Bearer JWT)
              CloudFront ─┴─ S3 (static Expo web export)
                          │
                    ┌─────▼──────────────────────────────┐
                    │  API service  (ECS: twain-api)      │  light image
                    │  FastAPI: auth · CRUD · SSE stream  │  (existing)
                    │  - validates Entra JWT              │
                    │  - reads/writes Postgres            │
                    │  - enqueues jobs, tails run_events  │
                    └───────┬───────────────────▲─────────┘
                            │ enqueue (jobs)     │ status + events
                            ▼                    │
                    ┌───────────────────────────┴─────────┐
                    │  Runner service (ECS: twain-runner)  │  heavy image
                    │  pixi env + modules/ + LLM creds     │  (NEW)
                    │  - claims a job                      │
                    │  - Orchestrator(request, agent,      │
                    │      ask=<db-bridge>, store=PgStore) │
                    │  - .run(until=…)                     │
                    │  - writes sessions + run_events      │
                    │  - `ask` suspends, resume drives     │
                    └───────┬──────────────────────────────┘
                            │
              RDS PostgreSQL (twaindb) ── shared state
              Entra ID (WashU SSO) ── user login + LLM creds
              WashU AI gateway (aiapi.wustl.edu) ── the model
```

### 4.1 Why a separate runner service (key decision)
The current `api` image is `python:3.12-slim` with light deps. The engine needs the **pixi environment** (pymatgen, ase, psutil, …), the `modules/` tree, the WashU LLM credentials, and can run for **many minutes** (EXECUTE alone allows 20 min) — potentially executing generated code. Running that inside the request-serving API task would bloat the image, block workers, and complicate scaling.

**Decision:** keep the API light and add a **runner** service built from the repo root (reusing `pixi.toml`). The API and runner share Postgres. When a run needs the researcher, the orchestrator's `ask` callable **suspends** it — the run is checkpointed to Postgres and the process released — rather than blocking a thread on a "pending question" row; the user's reply (via the API) enqueues a `resume` job that drives the run onward. One runner therefore serves many runs and nothing spins waiting on a human. *(Earlier drafts described a blocking `ask` that owned a run and waited on the pending-question row; that model was replaced by suspend/resume — see `runner/suspend.py` and `runner/README.md`.)*

**Coordination (MVP):** a `jobs` table in Postgres acts as the queue (the runner claims rows with `SELECT … FOR UPDATE SKIP LOCKED`). No SQS needed initially; SQS/EventBridge is a later hardening step.

### 4.2 Making engine state cloud-native
The engine defaults to SQLite (`logs/sessions.db`) + JSON files under `logs/`. For a multi-instance cloud deploy we implement **`PgStore`** with the *same interface* as the existing `Store` (`get_session` / `list_sessions` / `resume_session` / `save_session`) and inject it via the orchestrator's `store=` parameter. An event-bus subscriber writes lifecycle events into a `run_events` table that the API tails for SSE. No changes to the state machine itself.

---

## 5. Authentication & authorization

### 5.1 Flow (Entra ID OIDC, auth-code + PKCE)
1. Expo web app runs the OIDC auth-code + PKCE flow against WashU tenant `4ccca3b5-…` (via `expo-auth-session`).
2. The app receives an ID token + access token and sends the access token as `Authorization: Bearer` on every API call.
3. FastAPI validates the JWT (signature via tenant JWKS, `aud`, `iss`, `exp`) on each request.
4. On first successful call, the API **upserts a `users` row** keyed by the token's stable subject (`oid`), storing email/name and defaulting `role = 'user'`.
5. `require_admin` gates admin endpoints on `users.role = 'admin'`.

### 5.2 Two Entra app registrations required (manual, in WashU Azure portal)
> The engine already has a **confidential** app registration for the LLM (client-credentials). The web UI needs its own registrations. These require someone with WashU Azure AD app-registration rights.

- **API app registration** ("twain-api"): exposes an App ID URI / scope (e.g. `api://twain-api/access_as_user`). Its Application ID is the JWT **audience** the API validates.
- **SPA app registration** ("twain-web"): platform = SPA, redirect URIs for `http://localhost:8081` (dev) and `https://d1z5umg4xc2bl8.cloudfront.net` (prod, plus `twain.wustl.edu` when DNS is ready); requests the API scope above. Public client (PKCE, no secret).

### 5.3 API environment variables (names only — values via Secrets Manager / `.env`)
```
ENTRA_TENANT_ID        # 4ccca3b5-... (WashU tenant)
ENTRA_API_AUDIENCE     # Application ID URI or client id of the API app registration
ENTRA_ISSUER           # https://login.microsoftonline.com/<tenant>/v2.0  (derived if unset)
AUTH_DISABLED          # "true" only for local dev without Entra (injects a dev user)
BOOTSTRAP_ADMIN_EMAILS # optional comma-separated seed admins on first login
```
Frontend needs `EXPO_PUBLIC_ENTRA_CLIENT_ID`, `EXPO_PUBLIC_ENTRA_TENANT_ID`, `EXPO_PUBLIC_ENTRA_API_SCOPE`.

> **Security note:** the repo `.env` currently holds live gateway credentials (`API_KEY`, `CLIENT_SECRET`). Confirm `.env` is git-ignored, move these to Secrets Manager for the runner task, and rotate them.

---

## 6. Data model (Postgres, `twaindb`)

New tables (migration `api/migrations/001_web_ui.sql`). The engine's own session state lives in `sessions` via `PgStore`.

- **`users`** — `id` (uuid), `subject` (Entra oid, unique), `email`, `name`, `role` (`user`|`admin`), `created_at`, `last_login_at`.
- **`conversations`** — `id` (uuid = the run's `session_id`), `user_id` → users, `title`, `status`, `current_state`, `created_at`, `updated_at`. One conversation wraps one run.
- **`messages`** — `id`, `conversation_id` → conversations, `role` (`user`|`assistant`|`system`), `content`, `kind` (`chat`|`clarification`|`approval_request`|`approval_response`), `state` (state machine state at time of message), `created_at`.
- **`sessions`** — mirrors the engine's `RunSession` JSON (`session_id` PK, `researcher_id`, `state`, `status`, `data` JSONB, `updated_at`) so `PgStore` satisfies the existing `Store` interface.
- **`run_events`** — append-only lifecycle/provenance events for SSE: `id` (bigserial), `session_id`, `seq`, `event_type`, `payload` JSONB, `created_at`.
- **`jobs`** — the runner queue: `id`, `session_id`, `kind` (`start`|`resume`|`rerun`), `params` JSONB, `status` (`queued`|`claimed`|`running`|`done`|`error`), `claimed_at`, `created_at`.

---

## 7. API contract (by phase)

All endpoints require a valid Bearer token except `/api/health`. `me` = the authenticated user.

**Phase 0 (foundation)**
- `GET  /api/me` → current user (id, email, name, role).
- `GET  /api/admin/users` *(admin)* → list users.
- `PATCH /api/admin/users/{id}/role` *(admin)* → set role.

**Phase 1 (chat / Start Simulation)**
- `POST /api/conversations` `{ request }` → create conversation + enqueue `start` job → `{ id, status, state }`.
- `POST /api/conversations/{id}/messages` `{ content }` → append a user turn (answers a clarification / continues chat).
- `POST /api/conversations/{id}/approval` `{ decision, edits? }` → answer a plan-approval gate.
- `GET  /api/conversations/{id}` → full conversation state (messages, current_state, budget).
- `GET  /api/conversations/{id}/stream` → **SSE** of `run_events` (stage transitions, agent output, pending questions).

**Phase 2 (history + reports / Browse)**
- `GET  /api/conversations` → my conversations (via `Store.list_sessions(me)`).
- `GET  /api/conversations/{id}/report` → `ResultPackage`.
- `GET  /api/conversations/{id}/artifacts/{name}` → a stage artifact (intent_spec, execution_plan, …).
- `GET  /api/conversations/{id}/provenance` → hash-chained event log.

**Phase 3 (rerun from a state / Resume Workflow)**
- `GET  /api/conversations/{id}/states` → checkpointed states available to fork from.
- `POST /api/conversations/{id}/rerun` `{ from_state, edits? }` → fork a new conversation seeded from that checkpoint (optionally editing that state's artifact) and enqueue a `rerun` job.

**Phase 4 (admin)**
- `GET  /api/admin/conversations` *(admin)* → org-wide runs.
- `GET  /api/admin/costs` *(admin)* → spend from `BudgetTracker` / `ResourceUsage` (the Phase-8 cost dashboard).

---

## 8. Frontend

Add Expo Router routes and an auth context:
- `src/auth/` — `expo-auth-session` OIDC config, token storage, `useAuth()`, an axios request interceptor that attaches the Bearer token and refreshes on 401.
- Screens: **Login** (WashU SSO button), **Chat** (`/chat`, `/chat/[id]`) with a live 13-state stepper + inline clarification/approval, **Browse** (`/browse`) list + conversation detail with a **Report** view (Plotly per S1), **Rerun** (state timeline on the conversation), **Admin** (`/admin`, guarded).
- Gate the existing three tiles behind auth; wire Login to the SSO flow.

---

## 9. Phased delivery plan

### Phase 0 — Foundations (auth + data + engine seam)
- [x] `users / conversations / messages / sessions / run_events / jobs` migration (`api/migrations/001_web_ui.sql`).
- [x] `api/auth.py`: Entra JWT validation, `get_current_user` (+ user upsert), `require_admin`, `AUTH_DISABLED` dev mode.
- [x] `GET /api/me`, `GET /api/admin/users`, `PATCH /api/admin/users/{id}/role` + tests (26 passing, 85% cov, ruff clean).
- [ ] Frontend: OIDC login + token interceptor; wire the Login button; auth-gate the app. *(blocked on the SPA app registration client id — §5.2)*
- [ ] `PgStore` (same interface as `Store`) — proves engine state can live in Postgres.

### Phase 1 — Chat (Start Simulation) · *code complete; needs a live env to run E2E*
- [x] Runner service (`runner/`): claims `jobs`, drives the orchestrator, `ask` bridged to `messages`, approval gate at BUILD, `PgStore`, `PgEventSink` → `run_events`. Dockerfile (repo-root context) + `ci-runner.yml`. 11 unit tests (fakes; no DB/pixi needed).
- [x] API: `POST/GET /api/conversations`, `GET /{id}`, `POST /{id}/messages`, `POST /{id}/approval`, `GET /{id}/stream` (SSE). Owner-scoped. 37 tests total, 74% cov, ruff clean.
- [x] Chat screen (`ChatScreen.tsx`, route `/chat`): live 13-state stepper, clarification replies, inline plan approve/reject; "Start Simulation" tile wired. tsc + lint clean.
- [ ] Refresh `pixi.lock` (`pixi install`) after the psycopg2/boto3 additions, then provision the `twain-runner` ECS service + its deploy job.
- [ ] End-to-end run against live Postgres + WashU LLM (couldn't run in the dev sandbox: no Postgres/pixi/gateway).
- [ ] *(deferred to Phase 3)* edit-the-plan on approval; runner `resume`/`rerun` job kinds.

### Phase 2 — History + reports (Browse)
- [ ] List/detail/report/artifacts/provenance endpoints.
- [ ] Browse list + report view (Plotly).

### Phase 3 — Rerun from a state (Resume Workflow)
- [ ] `states` + `rerun` endpoints (fork from checkpoint, optional artifact edit).
- [ ] State-timeline UI + "rerun from here."

### Phase 4 — Admin
- [ ] Org-wide runs + cost dashboard; admin panel UI.

---

## 10. Security & operations
- Validate JWT signature/`aud`/`iss`/`exp` on every request; never trust client-supplied identity.
- Least privilege: the runner task role needs Secrets Manager (LLM creds) + RDS; the API task role needs RDS only.
- Move LLM credentials out of `.env` into Secrets Manager; rotate the exposed secret.
- Sandbox the EXECUTE stage (it runs generated code) — resource limits, no broad network/IAM on the runner.
- Enforce per-run and per-user budget caps (the engine already tracks them); surface in the cost dashboard.
- Extend CI: the runner needs its own build/deploy workflow; keep the existing two.

## 11. Open items
- Confirm who can create the two Entra app registrations (§5.2).
- Custom domains/DNS for `twain.wustl.edu` (+ CloudFront cert) and `twain-api.wustl.edu`.
- Runner scaling model (single task vs. autoscaled pool; queue backend when volume grows).

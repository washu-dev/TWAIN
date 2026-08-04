# TWAIN Web MVP — Delivery Plan

**Status:** Proposed
**Date:** 2026-07-16
**Owner:** (assign)
**Supersedes for MVP scope:** the phasing in [`../architecture/web_ui_plan.md`](../architecture/web_ui_plan.md) (still the canonical design reference for auth/data-model/API-contract).

---

## 1. Goal (what "done" means)

A researcher can, **from a web browser with nothing installed**:

1. Open the TWAIN website and sign in with a lightweight gate.
2. Type any natural-language request and chat with TWAIN (clarifications + plan approval inline).
3. Have TWAIN actually **run the simulation** and stream progress back.
4. See a list of their **past conversations** and open any of them.
5. View the **results/report** (summary + downloadable generated code and artifacts) for a finished run.

Explicitly **out of MVP scope** (tracked as follow-ons in §9): full WashU SSO, rerun-from-a-state, the admin panel, and the cost dashboard.

---

## 2. Decisions locked in

These were confirmed with the project owner and drive the plan:

| Decision | Choice | Consequence |
|---|---|---|
| **Auth for MVP** | **Interim lightweight auth** (email-based), not Entra SSO yet | Removes the external "two Entra app registrations" blocker from the critical path. Per-user identity still preserved so history is scoped per user. SSO becomes a later drop-in (backend already validates Entra JWTs). |
| **LLM gateway reachability** | **VPN/campus-only** (`aiapi.wustl.edu`) | **The runner cannot live on AWS ECS.** It must run on-campus where it can reach the gateway. Deployment topology changes to a **hybrid** (public edge on AWS, runner on-campus). |
| **Current deploy state** | **Only ever run locally via `dev.sh`** | Cloud bring-up is treated as real, unproven work. First successful cloud run is an explicit milestone, not an assumption. |

---

## 3. Target topology (hybrid: AWS public edge + on-campus runner)

Because the LLM is only reachable from the WashU network, we split the stack so that **only the runner** needs to sit on-campus, and it needs **no inbound connectivity** (it polls a queue). Everything the browser touches stays on AWS with easy public HTTPS.

```
        Browser (static Expo web, nothing installed)
              │  HTTPS
     CloudFront ─── S3  (app/ web export)                    ── AWS, public
              │
              ▼
     ALB (HTTPS) ─── ECS Fargate: twain-api (FastAPI)        ── AWS, public
              │  reads/writes
              ▼
     RDS PostgreSQL (twaindb)  ◄─── shared state / job queue ── AWS
              ▲
              │  OUTBOUND ONLY (poll jobs, write events/artifacts)
   ┌──────────┴───────────────────────────────────────────┐
   │  Runner (pixi env + modules/)                          │  ── ON-CAMPUS
   │  on a WashU host / lab VM / Compute2                    │     (WashU network)
   │  - reaches RDS outbound over TLS                        │
   │  - reaches the LLM gateway locally (campus network)     │
   └────────────────────────────────────────────────────────┘
              │
              ▼
     WashU AI gateway (aiapi.wustl.edu)  ── campus-only
```

**Why this shape (rationale):**
- The runner's design — claim jobs with `SELECT … FOR UPDATE SKIP LOCKED`, write results back — needs **only outbound** DB access. That makes it trivially firewall/VPN-friendly: no public inbound port on the campus host (the hard IT ask), just outbound to RDS:5432 and to the LLM.
- The public-facing pieces (frontend, API) keep the AWS setup the plan already provisioned — easy managed HTTPS, no campus inbound.
- The API never calls the LLM (only the runner does), so the API is unaffected by the VPN constraint.

**RDS access from campus — pick one (in order of preference):**
- **B1 (MVP-simplest):** RDS reachable from the campus egress IP, security-group-restricted to (a) the ECS API and (b) the WashU NAT/egress CIDR, **TLS-required**, strong rotated credentials.
- **B2 (hardening upgrade):** a tunnel instead of exposing RDS — AWS Client VPN, a WireGuard/SSH tunnel via a small bastion, or AWS SSM port-forwarding. Runner dials the tunnel; RDS stays private.

> Alternative considered — **Topology A: everything on-campus** (Postgres + API + runner all on a WashU host, only the frontend on CloudFront). Rejected for MVP: it forces **inbound public HTTPS on a campus host** (institutional DNS + TLS + firewall change), which is a heavier, slower IT dependency than campus-outbound-to-RDS. Keep A in reserve if outbound-to-RDS turns out to be blocked.

---

## 4. Current state → gap summary

| Capability | Built? | Gap for MVP |
|---|---|---|
| Chat loop (intake→clarify→plan→approve→execute→report) | ✅ code-complete | Validate E2E with real LLM; polish error/edge states |
| Conversations / messages / SSE / artifacts API | ✅ | SSE unused by app (polling works — acceptable) |
| History list + report view (Browse/Report screens) | ✅ | Validate; minor polish |
| Runner: `start` jobs, gates, PgStore, event sink, artifacts | ✅ | Relocate off ECS to on-campus; `resume`/`rerun` are Phase-3 (out of MVP) |
| DB schema + migrations | ✅ | Ensure applied to RDS |
| API auth backend (Entra JWT, roles, `AUTH_DISABLED`) | ✅ | Add **interim** auth path; wire frontend |
| Frontend auth / login | ❌ stub | Build interim login + token handling |
| Public deployment (frontend+API on AWS, runner on-campus) | ❌ never validated | The bulk of the work |
| Secrets off `.env` | ❌ | Move LLM creds to a campus secret store; rotate the exposed one |
| Stale docs (`api/README.md`, `app/QUICKSTART.md`, Slurm refs) | — | Fix as we touch each area |

---

## 5. Phased plan

Each phase lists **goal · tasks (with file pointers) · acceptance criteria · effort**. Effort is rough calendar sizing for one developer; phases 0–2 can partly overlap.

### Phase 0 — Local baseline & cleanup  ·  *~2–3 days*
**Goal:** a known-good, fully-working local reference before touching the cloud, and current docs.

Tasks:
- Run `./dev.sh` on the WUSTL VPN and complete a **real** chat end-to-end (prompt → clarify → approve → EXECUTE runs → report with artifacts). Capture the happy path and 2–3 failure paths (LLM error, rejected plan, execute failure).
- Fix stale docs discovered in the survey: [`api/README.md`](../../api/README.md) still documents only the greetings demo; [`app/QUICKSTART.md`](../../app/QUICKSTART.md) references `REACT_APP_API_BASE_URL` (code reads `EXPO_PUBLIC_API_BASE_URL`) and an outdated file tree; remove/relabel the **Slurm/Compute2** references in root [`README.md`](../../README.md) and [`docs/README.md`](../README.md) (no Slurm path is implemented in the runner).
- Remove the leftover `/api/greetings` demo endpoint + `query_greetings` once nothing depends on it ([`api/main.py`](../../api/main.py), [`api/database.py`](../../api/database.py)).

Acceptance: a recorded local run reaches a report; `pixi run test`, `pytest api/`, `pytest runner/tests` green; docs match reality.

### Phase 1 — Interim authentication  ·  *~3–5 days*
**Goal:** per-user sign-in with no external dependency, so history is correctly scoped and the site isn't wide open.

Design: email-based login that mints a short-lived signed **HS256** session JWT (server secret), stored client-side and sent as `Authorization: Bearer`. Each email maps to a `users` row (reusing the existing table), so per-user conversation scoping works unchanged. Structure it so Entra SSO later becomes an *additional* token issuer, not a rewrite.

Tasks (API — [`api/auth.py`](../../api/auth.py)):
- Add `POST /api/auth/login` accepting an email (validated against an **allowlist** env var, e.g. `INTERIM_ALLOWED_EMAILS` or an `@wustl.edu` domain check) → upsert `users` row → return an HS256 JWT signed with `INTERIM_JWT_SECRET`, short TTL + refresh.
- Extend `get_current_user` to accept interim tokens (verify HS256 with the server secret) **in addition to** the existing Entra RS256 path and `AUTH_DISABLED`. Keep `require_admin` working off `users.role`.
- Tests mirroring `test_auth.py` (valid/expired/forged token, non-allowlisted email → 401/403).

Tasks (Frontend — [`app/src/api/client.ts`](../../app/src/api/client.ts), `app/src/screens/`):
- Real Login screen/modal (replace the HomeScreen stub at [`HomeScreen.tsx`](../../app/src/screens/HomeScreen.tsx)): email entry → `POST /api/auth/login` → store token (web `localStorage`) → call the already-present `apiClient.setAuthToken()`.
- Add a 401 interceptor that clears the token and routes to Login; gate `/chat`, `/browse`, `/report` behind having a token.

Acceptance: with `AUTH_DISABLED=false`, an allowlisted user logs in, chats, and sees **only their own** conversations; a second user sees a separate history; unauthenticated requests get 401.

### Phase 2 — History + results polish  ·  *~2–3 days*
**Goal:** confirm and round out "view old chats + results" (largely built already).

Tasks:
- Verify [`BrowseScreen.tsx`](../../app/src/screens/BrowseScreen.tsx) list, status badges, and routing (active→`/chat`, terminal→`/report`) against real multi-run data.
- Verify [`ReportScreen.tsx`](../../app/src/screens/ReportScreen.tsx): summary card fields (method, cost/compute estimate, execution status) populate from real artifacts; artifact expand/download works, including `run_bundle/main.py`.
- Empty/error/loading states: no runs yet, run still in progress, report for a failed/rejected run.
- (Optional, low cost) expose `GET /api/conversations/{id}/provenance` (hash-chained event log) — nice for trust, not required for MVP.

Acceptance: a user with several past runs can browse them and open a correct report for each terminal run.

### Phase 3 — Deployment (hybrid topology)  ·  *~1–2 weeks (dominant phase)*
**Goal:** the site is reachable on the public web and completes a real run, with the runner on-campus.

**3a. Public edge on AWS**
- Frontend: confirm/repair the existing [`deploy-app.yml`](../../.github/workflows/deploy-app.yml) pipeline (Expo web export → S3 `twain-dev-1781888831` → CloudFront). Set `EXPO_PUBLIC_API_BASE_URL` (build-time) to the public API URL. Verify the CloudFront site loads.
- API: confirm/repair [`deploy-api.yml`](../../.github/workflows/deploy-api.yml) (Docker → ECR → ECS `twain-api`). Put an **ALB + ACM cert** in front for HTTPS (or CloudFront/API Gateway). Update CORS in [`api/main.py`](../../api/main.py) to the real origin(s). Target the planned domain `twain-api.wustl.edu` when DNS is ready.
- RDS: ensure `twaindb` exists and **migrations are applied** (`api/migrations/*.sql`). Confirm the API task reaches RDS in-VPC.

**3b. Runner on-campus (the topology change)**
- Stand up a WashU host with the pixi env (a lab VM, workstation, or Compute2 login node) — anywhere with LLM-gateway access. Not ECS.
- Configure it as an outbound worker: `DB_HOST/PORT/NAME/USER/PASSWORD` → RDS; `TWAIN_EXECUTE_LOCALLY=1`; LLM creds from a campus secret store (not committed `.env`).
- Enable RDS access from campus per **§3 B1** (SG allowlist + TLS) or **B2** (tunnel).
- Run as a durable service (systemd unit / `pixi run python -m runner.runner`, or the runner Docker image built for the host arch). Retire/disable the ECS `twain-runner` deploy path in [`ci-runner.yml`](../../.github/workflows/ci-runner.yml) (keep the build/test job; drop or gate the ECS deploy).

**3c. First cloud run (milestone)**
- From the public site, log in and run a simple request end-to-end; watch it flow through the on-campus runner and back into the report. Fix connectivity/auth/CORS/secret issues as they surface.

Acceptance: an external user (allowlisted) opens the CloudFront URL, signs in, submits a prompt, approves the plan, and gets a report — with the runner executing on-campus.

### Phase 4 — Production readiness & hardening  ·  *~1 week*
**Goal:** safe to leave running for real users.

Tasks:
- **EXECUTE sandboxing:** the runner runs generated code — constrain it (containerized execution via the existing Docker offload, resource/time limits, minimal network/filesystem, no cloud credentials on the runner host beyond RDS+LLM). See §10 in [`web_ui_plan.md`](../architecture/web_ui_plan.md).
- **Secrets:** move LLM creds off `.env` into a campus secret store; **rotate** the currently-exposed `CLIENT_SECRET`/`API_KEY`.
- **Budget caps:** the engine already tracks per-run/per-user budget — enforce hard caps and surface remaining budget in the UI so a runaway prompt can't burn unbounded LLM spend.
- **Observability:** ship API + runner logs somewhere queryable (CloudWatch for API; campus log location for the runner); a `/api/health` uptime check; alert on stuck `jobs` rows (claimed but never completed).
- **Resilience:** runner auto-restart (systemd), job-claim staleness reaper (re-queue jobs claimed by a dead runner), sensible SSE/poll timeouts.
- **UX:** friendly error surfacing in chat when a run errors; a clear "waiting for your approval" indicator (the README notes idle-looking runs are often awaiting approval).

Acceptance: a killed runner recovers without losing/duplicating jobs; a bad prompt fails gracefully with a user-visible message; no plaintext secrets in the repo or images; a per-user budget cap is enforced.

---

## 6. Critical path & sequencing

```
Phase 0 (local baseline) ──► Phase 1 (interim auth) ─┐
                          └─► Phase 2 (history polish)┤
                                                      ▼
                                   Phase 3 (deploy: 3a edge ∥ 3b runner ─► 3c first run)
                                                      ▼
                                   Phase 4 (hardening)  ──► MVP launch
```

- **Longest pole:** Phase 3b (on-campus runner + RDS connectivity) — start the WashU-host/networking request **at the beginning of Phase 0**, since provisioning a campus VM and opening egress-to-RDS may involve IT lead time.
- Phases 1 and 2 are independent and can run in parallel.
- Phase 4's secret rotation should happen **before** any public exposure in 3c, not after.

---

## 7. External dependencies (not fully in the dev's control — start early)

| Dependency | Needed for | Owner |
|---|---|---|
| A WashU host/VM with LLM-gateway access + outbound-to-RDS | Phase 3b | WashU IT / lab |
| RDS reachable from campus (SG allowlist or tunnel) | Phase 3b | AWS admin + WashU network |
| Public HTTPS for the API (ALB+ACM, or domain `twain-api.wustl.edu`) | Phase 3a | AWS admin / WashU DNS |
| A campus secret store (or documented secure `.env` handling) | Phase 4 | project |
| Confirmed AWS access (the account/resources in `web_ui_plan.md` §2.1) | Phase 3 | AWS admin |

---

## 8. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Campus egress to RDS:5432 blocked | Runner can't reach the queue → no runs | Fall back to tunnel (B2), or Topology A (all-on-campus + inbound HTTPS) |
| LLM gateway also blocks the specific host | No runs | Verify from the exact host early in Phase 0; the endpoint is overridable via `TWAIN_AGENT_ENDPOINT` |
| Generated code (EXECUTE) does something unsafe on the campus host | Security | Phase 4 sandboxing is mandatory before public exposure; use Docker offload with resource/network limits |
| Interim auth is too weak (open email allowlist) | Abuse / cost | Restrict allowlist tightly for launch; budget caps (Phase 4); prioritize SSO (§9) |
| Long runs vs. SSE/poll timeouts | Truncated UI updates | Polling already tolerant; tune `SSE_MAX_SECONDS`; the runner persists state regardless of client connection |
| Single runner = throughput bottleneck | Queued runs pile up | `SKIP LOCKED` already supports multiple runners; add a second campus worker if needed |

---

## 9. Follow-ons after MVP (from `web_ui_plan.md` phases 0/3/4)

- **WashU SSO (Entra):** create the two app registrations (API + SPA), wire `expo-auth-session` on the frontend, and register the Entra token issuer alongside the interim one. Backend validation already exists.
- **Rerun from a state (Resume Workflow):** implement the `resume`/`rerun` job kinds in the runner (currently `NotImplementedError` — [`runner/runner.py`](../../runner/runner.py)); the engine primitives (`run(until=)`, checkpoint resume) already exist. Add the `states` + `rerun` endpoints and the timeline UI.
- **Admin panel + cost dashboard:** org-wide runs and spend views; the user-role API endpoints already exist.
- **SSE in the frontend:** switch chat from polling to the existing `/stream` endpoint if update latency/scale warrants.

---

## 10. Definition of Done (MVP)

- [ ] Public CloudFront URL loads with nothing installed client-side.
- [ ] An allowlisted user logs in (interim auth) and sees only their conversations.
- [ ] A free-text prompt runs end-to-end (clarify → approve → **execute on-campus** → report).
- [ ] Browse lists past runs; Report shows summary + downloadable artifacts for terminal runs.
- [ ] Runner runs on-campus; API/frontend/DB on AWS; no plaintext secrets; exposed secret rotated.
- [ ] EXECUTE is sandboxed and per-user budget caps are enforced.
- [ ] A killed runner recovers without losing or duplicating jobs.

# TWAIN web app

The researcher-facing client. Built with Expo SDK 56 (expo-router), React 19,
React Native 0.85 and react-native-web, in TypeScript, and shipped as a static
web build. It talks only to the [API](../api/README.md); it never reaches the
pipeline, the database or RIS directly.

Served from S3 bucket `twain-dev-1781888831` through CloudFront
`E3PINHJ1G0F5PS` at **https://d1z5umg4xc2bl8.cloudfront.net**. The same
distribution sends `/api/*` to the API. The
[run lifecycle diagram](../docs/architecture/08_run_lifecycle.drawio) shows how
each screen element is fed.

> Expo changes quickly: read the versioned docs at
> https://docs.expo.dev/versions/v56.0.0/ before changing app code (see `AGENTS.md`).

## Run it

```bash
cd app
cp .env.example .env        # point EXPO_PUBLIC_API_BASE_URL at your API
npm install
npm run web                 # http://localhost:3001
```

`../dev.sh` starts it together with a local API and runner.

| Variable | Meaning |
|---|---|
| `EXPO_PUBLIC_API_BASE_URL` | API origin (default `http://localhost:8000`) |
| `EXPO_PUBLIC_ENTRA_TENANT_ID` / `_CLIENT_ID` | Microsoft Entra sign-in (one SPA app registration) |
| `EXPO_PUBLIC_ENTRA_API_SCOPE` | Optional. Unset = ID-token mode: the ID token is the bearer, and the API accepts the client id as its audience |
| `EXPO_PUBLIC_AUTH_DISABLED` | Skip sign-in (only against an API with `AUTH_DISABLED=true`) |
| `EXPO_PUBLIC_APP_VERSION` | Set by the build from `app/VERSION` and shown in the footer |

**Sign-in** is Entra OIDC auth-code + PKCE through `expo-auth-session`
(`src/hooks/useAuth.tsx`), with silent refresh. `src/api/client.ts` is an axios
client that attaches the bearer token and handles 401s. The MSAL packages in
`package.json` are unused.

## Screens and routes (`src/app/` → `src/screens/`)

| Route | Screen | What it does |
|---|---|---|
| `/` | Landing | Public home; signed-in users go to the dashboard |
| `/login` | Login | Entra sign-in |
| `/dashboard` | Dashboard | Start a run; recent runs |
| `/chat`, `/conversations/[id]` | **Chat** | The run window (below) |
| `/browse` | Browse | All my runs and their statuses (polls while any are active) |
| `/report` | Report | A finished run's headline result, summary and artifacts |
| `/libraries` | Libraries | What the cluster can run, from the worker's RIS inventory |
| `/settings` | Settings | Email notification preferences, API health and version |
| `/tutorial` | Tutorial | A worked example built from the real components |

## The run window

Everything comes from the API:
- the transcript, from `GET /api/conversations/{id}`;
- live state, from SSE (`useConversationStream`, with a polling fallback);
- in-stage activity, from `GET /activity` every 2 s (`useRunActivity`).

| Component | Shows |
|---|---|
| **`RunTracker`** (the pizza tracker) | Five phases: **Plan** (INTAKE…PLAN), **Build** (BUILD, REPAIR), **Run on RIS** (EXECUTE), **Check** (INTERPRET, VALIDATE, CORRECT, REPLAN), **Results** (ACCEPT, TERMINATE). Each of the 14 states belongs to one phase, so the tracker never goes blank. The current phase pulses (honouring Reduce Motion). One line says what is happening now ("Waiting for a node: Priority", "Running on c2-node-003"); during the approval gate, Plan is the phase highlighted. |
| **`RunActivity`** | The subtask checklist for the current stage (e.g. EXECUTE: stage → preflight → submit → queue → run → fetch), live elapsed time and the job's stdout tail |
| **`PlanCard`** | The plan at the approval gate: method, system, cost estimate and the Slurm request (CPUs, GPUs, RAM, wall time; editable, with `WallTimeField`) |
| **`FailureCard`** | Where and why a run stopped, including the job's final exception and stdout/stderr. Buttons: **Re-run from BUILD / PLAN** (opens the re-run editor), **Download run bundle / outputs** (`/run-files`), **Reproduce on RIS** (commands that fetch the bundle and run it in the same environment) |
| `ReportIssueModal` · `IssueModal` | File a run report · a plain issue (e.g. "provision this engine") |
| `Footer` | App version beside the API version |

## Build and deploy

`.github/workflows/deploy-app.yml` runs on changes under `app/**`:
- **Checks:** `npm ci`, `tsc --noEmit`, `expo lint`, the npm audit gate
  (`scripts/audit-gate.mjs` with `audit-allowlist.json`) and a licence check.
- **Deploy** (on `master`):
  1. Read the Entra identifiers through the `TWAIN-sso-ci-reader` role
     (`TWAIN/sso/APP_ID`, `TENANT_ID`); `API_BASE_URL` comes from a
     repository secret.
  2. Bump `app/VERSION` (`YYYY.MM.DD.NNN`, independent of the API's version).
  3. `expo export --platform web`, then `aws s3 sync dist/` (hashed assets
     immutable, HTML no-cache).
  4. Invalidate CloudFront, then commit the release and tag `app-v…`.

Check locally before a PR:

```bash
npx tsc --noEmit && npx expo lint && npm run build
```

# TWAIN Web App — Quick Start

Expo + React Native Web app for TWAIN. Runs on Web (the deployment target), iOS,
and Android from one codebase. Talks to the [`../api`](../api) FastAPI backend.

> The fastest way to run the whole stack (DB + API + runner + app) is the
> repo-root [`../dev.sh`](../dev.sh). This guide is for running the app alone.

## Installation

```bash
cd app
npm install
```

## Environment

The app reads **`EXPO_PUBLIC_API_BASE_URL`** (Expo inlines `EXPO_PUBLIC_*` at
build time). Defaults to `http://localhost:8000` when unset.

```bash
# local
EXPO_PUBLIC_API_BASE_URL=http://localhost:8000

# production build
EXPO_PUBLIC_API_BASE_URL=https://twain-api.wustl.edu
```

Auth: set **`EXPO_PUBLIC_AUTH_DISABLED=true`** to skip login locally (pair it
with the API's `AUTH_DISABLED=true`). Without it, the app runs WashU SSO — see
[WashU SSO (Entra ID)](#washu-sso-entra-id) below. Full app-side config lives in
[`.env.example`](.env.example).

## Running

```bash
npm run web        # http://localhost:3001
npm run ios        # iOS simulator (macOS)
npm run android    # Android emulator
npm start          # interactive picker
```

## Building for web

```bash
npm run build      # expo export --platform web → dist/
```

CI ([`../.github/workflows/deploy-app.yml`](../.github/workflows/deploy-app.yml))
runs `tsc`, `expo lint`, and an audit, then exports and syncs `dist/` to
S3/CloudFront on push to `master`.

## WashU SSO (Entra ID)

Sign-in uses Microsoft Entra ID (WUSTL Key) via the OIDC **auth-code + PKCE**
flow (`expo-auth-session`). The app obtains an access token and sends it as a
`Bearer` header; the API validates it ([`../api/auth.py`](../api/auth.py)). The
backend is already built — SSO just needs two Azure app registrations and config.

**1. Create two app registrations** (needs WashU Azure AD app-registration rights):

- **`twain-api`** — exposes an API scope, e.g. `api://twain-api/access_as_user`.
  Its Application (client) ID is the JWT **audience** the API validates.
- **`twain-web`** — platform **Single-page application (SPA)** (public client,
  PKCE, no secret). Add these **redirect URIs** (the app prints the exact value
  it uses to the dev console on startup — register it verbatim):
  - `http://localhost:3001` — local web dev
  - `https://d1z5umg4xc2bl8.cloudfront.net` — prod (plus `twain.wustl.edu` later)
  - `twain://` — native (iOS/Android)

  Under **API permissions**, add and grant the `twain-api` scope above.

**2. Configure the app** — copy [`.env.example`](.env.example) to `app/.env`:

```bash
EXPO_PUBLIC_ENTRA_CLIENT_ID=<twain-web SPA client id>
EXPO_PUBLIC_ENTRA_API_SCOPE=api://twain-api/access_as_user
# EXPO_PUBLIC_ENTRA_TENANT_ID defaults to WashU's tenant in code
```

**3. Configure the API** — in the repo-root `.env` (see [`../.env.example`](../.env.example)),
unset `AUTH_DISABLED` and set:

```bash
ENTRA_TENANT_ID=4ccca3b5-71cd-4e6d-974b-4d9beb96c6d6
ENTRA_API_AUDIENCE=<twain-api application id / App ID URI>
```

**4. Run without the dev bypass** — `dev.sh` disables auth for convenience, so to
exercise real SSO run the API and app with auth enabled, e.g.:

```bash
# API (from repo root): AUTH_DISABLED unset, ENTRA_* set
( cd api && ENTRA_TENANT_ID=… ENTRA_API_AUDIENCE=… .venv-api/bin/python -m uvicorn main:app --port 8000 )
# App (from app/): no EXPO_PUBLIC_AUTH_DISABLED
npm run web
```

`AUTH_DISABLED`/`EXPO_PUBLIC_AUTH_DISABLED` remain the local shortcut when you
don't need to test sign-in. Entra access tokens are short-lived; the app keeps
the refresh token (when `offline_access` is granted) to renew silently on load.

## Project structure

```
app/
├── src/
│   ├── app/            # Expo Router routes (_layout, index, login, dashboard, chat, browse, report)
│   ├── screens/        # LandingScreen, LoginScreen, DashboardScreen, ChatScreen, BrowseScreen, ReportScreen
│   ├── components/     # Header, Footer, TileButton, MessageModal, WashUShield
│   ├── api/            # client.ts (axios) + storage.ts (token persistence)
│   ├── hooks/          # useAuth (auth context), useColorScheme
│   ├── constants/      # theme.ts (Colors, Spacing, API_CONFIG, strings)
│   └── global.css      # web-only global styles
├── app.json · package.json · tsconfig.json
```

## Features

- **Home** (`/`) — public landing page describing TWAIN; the only way forward is
  to sign in. Redirects to `/dashboard` once authenticated.
- **Auth guard** — Expo Router `Stack.Protected` gates the app behind WashU SSO
  (Entra ID, `useAuth`); the app routes (`/dashboard`, `/chat`, `/browse`,
  `/report`) are unreachable until sign-in, and a 401 from the API signs the
  user out. See [WashU SSO (Entra ID)](#washu-sso-entra-id).
- **Chat** (`/chat`) — the conversational simulation flow with a live state
  stepper, clarification replies, and inline plan approve/reject.
- **Browse** (`/browse`) — past/active runs; **Report** (`/report`) — run summary
  and downloadable artifacts.
- **Header** — WashU branding, a `Test` button (calls `/api/health`), and a
  sign-out button.

## WashU branding

Colors from <https://marcomm.washu.edu/brand-color-palette/> (see
`src/constants/theme.ts`):

- **Primary** — WashU Red `#BA0C2F`
- **Secondary** — WashU Green `#215732`

## Development notes

- **Language:** TypeScript (`tsconfig.json`). Path alias `@/*` → `src/*`.
- **Styling:** React Native `StyleSheet` (cross-platform).
- **State:** React `useState`/context. No Redux/Zustand yet.
- **Expo SDK 56** — read the versioned docs at
  <https://docs.expo.dev/versions/v56.0.0/> before writing code (see `AGENTS.md`).

## Troubleshooting

- API connection: verify `http://localhost:8000/api/health` responds and that
  `EXPO_PUBLIC_API_BASE_URL` points at it; CORS must allow the app origin.
- Stuck on the login screen locally: set `EXPO_PUBLIC_AUTH_DISABLED=true`, or
  configure `INTERIM_JWT_SECRET` on the API.
- Cache issues: `npm start -- --reset-cache`.

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

Auth: set **`EXPO_PUBLIC_AUTH_DISABLED=true`** to skip the login screen locally
(pair it with the API's `AUTH_DISABLED=true`). Without it, the app shows the
interim email login and expects the API to have `INTERIM_JWT_SECRET` configured.

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
- **Auth guard** — Expo Router `Stack.Protected` gates the app behind interim
  email login (`useAuth`); the app routes (`/dashboard`, `/chat`, `/browse`,
  `/report`) are unreachable until sign-in, and a 401 from the API signs the
  user out.
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

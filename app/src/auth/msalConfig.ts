/**
 * Microsoft Entra ID (Azure AD) SSO configuration for the TWAIN web SPA.
 *
 * WashU federated login via the Microsoft identity platform. This is a public
 * client (SPA) using the OAuth 2.0 authorization-code flow with PKCE — there is
 * NO client secret. The tenant/client IDs below are supplied via git-ignored
 * `EXPO_PUBLIC_*` env vars (see `app/.env.example`); they ship in the browser
 * bundle and are not secrets, but are kept out of git per repo policy.
 *
 * The Entra app registration MUST use platform type "Single-page application
 * (SPA)" with the redirect URI(s) registered exactly (protocol, host, port, path).
 */
import { Platform } from 'react-native';
import { LogLevel, type Configuration, type RedirectRequest } from '@azure/msal-browser';

const tenantId = process.env.EXPO_PUBLIC_AZURE_TENANT_ID;
const clientId = process.env.EXPO_PUBLIC_AZURE_CLIENT_ID;

/** True only when both required Entra identifiers are present. */
export const isAuthConfigured = Boolean(tenantId && clientId);

/** Browser origin (e.g. http://localhost:8081 in dev). Undefined off-web. */
const origin =
  Platform.OS === 'web' && typeof window !== 'undefined'
    ? window.location.origin
    : undefined;

export const msalConfig: Configuration = {
  auth: {
    clientId: clientId ?? '',
    // Single-tenant WashU authority. Swap the tenant segment for `organizations`
    // or `common` only if multi-tenant sign-in is ever required.
    authority: `https://login.microsoftonline.com/${tenantId ?? 'common'}`,
    // Defaults to the app's own origin, which must be a registered SPA redirect
    // URI. Override per-environment with EXPO_PUBLIC_AZURE_REDIRECT_URI.
    redirectUri: process.env.EXPO_PUBLIC_AZURE_REDIRECT_URI || origin,
    postLogoutRedirectUri:
      process.env.EXPO_PUBLIC_AZURE_POST_LOGOUT_REDIRECT_URI || origin,
  },
  cache: {
    // localStorage persists the session across tabs/reloads for a smoother UX.
    // Switch to 'sessionStorage' if you prefer sign-in state cleared on tab close.
    cacheLocation: 'localStorage',
  },
  system: {
    loggerOptions: {
      loggerCallback: (level, message, containsPii) => {
        if (containsPii) return;
        if (level === LogLevel.Error) console.error('[MSAL]', message);
        else if (__DEV__ && level === LogLevel.Warning) console.warn('[MSAL]', message);
      },
      logLevel: __DEV__ ? LogLevel.Warning : LogLevel.Error,
    },
  },
};

/** Authenticate-only: request the user's identity, no API access token. */
export const loginRequest: RedirectRequest = {
  scopes: ['openid', 'profile', 'email'],
};

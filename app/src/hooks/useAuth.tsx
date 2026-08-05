import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import * as AuthSession from 'expo-auth-session';
import * as WebBrowser from 'expo-web-browser';
import { apiClient, AuthUser } from '@/api/client';
import { clearToken, loadTokens, saveTokens } from '@/api/storage';
import { ENTRA_CONFIG, isEntraConfigured } from '@/constants/theme';

// Finishes an auth session opened in a popup/redirect (web) by delivering the
// result back to the app and closing the popup. Safe to call unconditionally.
WebBrowser.maybeCompleteAuthSession();

// When EXPO_PUBLIC_AUTH_DISABLED is "true" (e.g. local dev.sh, which also runs the
// API with AUTH_DISABLED) the app skips SSO entirely — matching the API bypass.
const AUTH_DISABLED = process.env.EXPO_PUBLIC_AUTH_DISABLED === 'true';

// OIDC scopes. openid/profile/email identify the user; offline_access asks for a
// refresh token; the API scope (when set) makes the access token's audience the
// TWAIN API.
const BASE_SCOPES = ['openid', 'profile', 'email', 'offline_access'];

// With no API scope configured we can't mint an access token audienced for the
// TWAIN API, so the app runs in "ID-token mode": it sends the ID token (whose
// audience is this SPA's client id — the API's ENTRA_API_AUDIENCE) as the bearer.
// This needs no exposed API scope and no admin consent. With a scope set, the
// access token is used instead (the standard resource-token flow).
const USE_ID_TOKEN = !ENTRA_CONFIG.apiScope;

// Discovery-fallback timing. The wait lets useAutoDiscovery win normally, so the
// happy path makes no extra request; the retries cover a transient failure, which
// the hook itself does not.
const DISCOVERY_FALLBACK_AFTER_MS = 2500;
const DISCOVERY_RETRY_BACKOFF_MS = 2000;
const DISCOVERY_ATTEMPTS = 3;
// Absolute ceiling on the splash. Beyond this the app is shown regardless, because
// an indefinite spinner is the one outcome from which the user cannot recover
// without reloading the tab.
const AUTH_LOADING_CEILING_MS = 12000;

interface AuthContextValue {
  isAuthenticated: boolean;
  // Initial validation of a stored session (blocks the app briefly on load).
  isLoading: boolean;
  // An interactive sign-in / token exchange is in flight.
  isSigningIn: boolean;
  authDisabled: boolean;
  // Whether SSO is configured (client id present).
  authConfigured: boolean;
  // Whether the sign-in request is built and ready to launch.
  canSignIn: boolean;
  user: AuthUser | null;
  error: string | null;
  signIn: () => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function errMessage(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (e instanceof Error && e.message) return e.message;
  return fallback;
}

// Whether the bearer JWT expires within `skewSeconds`. Used to renew the token
// *before* a request goes out with a stale one; unparseable tokens are treated
// as "not expiring" so the request proceeds and the 401 path decides.
function expiresSoon(jwt: string, skewSeconds = 120): boolean {
  try {
    const payload = JSON.parse(
      atob(jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')),
    );
    return (
      typeof payload.exp === 'number' &&
      payload.exp * 1000 < Date.now() + skewSeconds * 1000
    );
  } catch {
    return false;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSigningIn, setIsSigningIn] = useState(false);
  // Only block on load when there might be a stored session to validate.
  const [isLoading, setIsLoading] = useState(() => {
    if (AUTH_DISABLED) return false;
    const { accessToken, refreshToken } = loadTokens();
    return Boolean(accessToken || refreshToken);
  });

  const authConfigured = isEntraConfigured();

  // OIDC discovery + PKCE auth request. Hooks must run unconditionally; when SSO
  // is unconfigured `request` simply stays unusable and `signIn` no-ops.
  const discovery = AuthSession.useAutoDiscovery(ENTRA_CONFIG.authority);
  // useAutoDiscovery returns null until it succeeds and, per the SDK reference,
  // defines no retry if the fetch fails -- so one slow or failed discovery request
  // left it null for the whole page load. Everything that needs it then stalled:
  // the silent refresh below returned early WITHOUT clearing isLoading, and
  // _layout renders a splash while isLoading, so the user sat on an indefinite
  // spinner. Reloading the tab was the only way in, which is exactly the "have to
  // refresh to get in sometimes" report. Fetch it ourselves as a bounded retrying
  // fallback, and use the result everywhere -- including useAuthRequest, or
  // interactive sign-in would stay broken for the same reason.
  const [fallbackDiscovery, setFallbackDiscovery] =
    useState<AuthSession.DiscoveryDocument | null>(null);
  const effectiveDiscovery = discovery ?? fallbackDiscovery;

  const redirectUri = useMemo(
    () => AuthSession.makeRedirectUri({ scheme: 'twain' }),
    [],
  );
  const scopes = useMemo(
    () => (ENTRA_CONFIG.apiScope ? [...BASE_SCOPES, ENTRA_CONFIG.apiScope] : BASE_SCOPES),
    [],
  );

  const [request, response, promptAsync] = AuthSession.useAuthRequest(
    {
      clientId: ENTRA_CONFIG.clientId,
      scopes,
      redirectUri,
      responseType: AuthSession.ResponseType.Code,
      usePKCE: true,
      prompt: AuthSession.Prompt.SelectAccount,
    },
    effectiveDiscovery,
  );

  // The fallback fetch. Waits briefly for useAutoDiscovery to win on its own (the
  // common case), then takes over with a bounded number of retries so a single
  // transient failure is not fatal to the whole session.
  useEffect(() => {
    if (AUTH_DISABLED || !authConfigured || effectiveDiscovery) return;
    let cancelled = false;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout>;
    const tryFetch = () => {
      const delay = attempt === 0
        ? DISCOVERY_FALLBACK_AFTER_MS
        : DISCOVERY_RETRY_BACKOFF_MS * attempt;
      timer = setTimeout(async () => {
        if (cancelled) return;
        attempt += 1;
        try {
          const doc = await AuthSession.fetchDiscoveryAsync(ENTRA_CONFIG.authority);
          if (!cancelled) setFallbackDiscovery(doc);
        } catch {
          if (!cancelled && attempt < DISCOVERY_ATTEMPTS) tryFetch();
        }
      }, delay);
    };
    tryFetch();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [authConfigured, effectiveDiscovery]);

  // Last resort, and the actual invariant: isLoading must always resolve. Every
  // early return in the validation effect below leaves it true on the assumption
  // that the effect will re-run with what it was missing -- and if that never
  // arrives, _layout holds a spinner forever and the only way in is reloading the
  // tab. Showing the app (landing/login) beats a dead splash. Stored tokens are
  // deliberately left intact, so a session that is merely slow still restores
  // itself once the request it was waiting on lands.
  useEffect(() => {
    if (!isLoading) return;
    const timer = setTimeout(() => {
      setIsLoading(false);
      setError((prev) => prev ?? 'Could not reach the sign-in service — please try again.');
    }, AUTH_LOADING_CEILING_MS);
    return () => clearTimeout(timer);
  }, [isLoading]);

  // Surface the resolved redirect URI in dev so it can be registered verbatim on
  // the SPA app registration (Entra requires an exact match).
  useEffect(() => {
    if (__DEV__ && !AUTH_DISABLED) {
      // eslint-disable-next-line no-console
      console.log(`[auth] Entra redirect URI (register this exactly): ${redirectUri}`);
    }
  }, [redirectUri]);

  // Keep the API client's bearer token in sync (used by the interceptor).
  const applyToken = useCallback((token: string | null) => {
    apiClient.setAuthToken(token);
  }, []);

  // Persist a freshly obtained token pair and resolve the user from the API.
  // Throws (leaving cleanup to the caller) if the API rejects the token.
  const establishSession = useCallback(
    async (accessToken: string, refreshToken: string | null) => {
      saveTokens({ accessToken, refreshToken });
      applyToken(accessToken);
      const me = await apiClient.me();
      setUser(me);
      setError(null);
    },
    [applyToken],
  );

  const resetSession = useCallback(() => {
    clearToken();
    applyToken(null);
    setUser(null);
  }, [applyToken]);

  // Silently mint a fresh bearer from the stored refresh token. Deduplicated:
  // concurrent callers (several in-flight requests hitting expiry at once)
  // share one round-trip to Entra. Returns the new bearer, or null when the
  // session can't be recovered (no refresh token, or Entra rejected it).
  const refreshInFlight = useRef<Promise<string | null> | null>(null);
  const refreshSession = useCallback(async (): Promise<string | null> => {
    const { refreshToken } = loadTokens();
    if (!refreshToken || !effectiveDiscovery || !ENTRA_CONFIG.clientId) return null;
    if (!refreshInFlight.current) {
      refreshInFlight.current = (async () => {
        try {
          const refreshed = await AuthSession.refreshAsync(
            { clientId: ENTRA_CONFIG.clientId, refreshToken, scopes },
            effectiveDiscovery,
          );
          const bearer = USE_ID_TOKEN ? refreshed.idToken : refreshed.accessToken;
          if (!bearer) return null;
          saveTokens({
            accessToken: bearer,
            refreshToken: refreshed.refreshToken ?? refreshToken,
          });
          applyToken(bearer);
          return bearer;
        } catch {
          return null;
        } finally {
          refreshInFlight.current = null;
        }
      })();
    }
    return refreshInFlight.current;
  }, [effectiveDiscovery, scopes, applyToken]);

  // Keep the session alive across access-token expiry (the "logged out after a
  // few minutes idle" bug): each request renews a nearly-expired token up front,
  // and a 401 triggers one refresh-and-retry (client.ts) — the session only
  // ends when the refresh token itself is gone or rejected.
  useEffect(() => {
    if (AUTH_DISABLED) return;
    apiClient.setTokenProvider(async () => {
      const { accessToken } = loadTokens();
      if (accessToken && !expiresSoon(accessToken)) return accessToken;
      return (await refreshSession()) ?? accessToken;
    });
    apiClient.setRefreshHandler(() => refreshSession());
    return () => {
      apiClient.setTokenProvider(null);
      apiClient.setRefreshHandler(null);
    };
  }, [refreshSession]);

  // On an unrecoverable 401 (refresh failed too), drop the session so the
  // guard routes to login.
  useEffect(() => {
    apiClient.setUnauthorizedHandler(() => {
      resetSession();
    });
    return () => apiClient.setUnauthorizedHandler(null);
  }, [resetSession]);

  // Validate a stored session on load; silently refresh the access token if it
  // has expired and a refresh token is available. Re-runs once discovery loads.
  useEffect(() => {
    if (AUTH_DISABLED) return;
    const { accessToken, refreshToken } = loadTokens();
    // isLoading is initialised false when there's nothing to validate, so this
    // early return leaves the app unblocked without a redundant setState.
    if (!accessToken && !refreshToken) return;
    let cancelled = false;
    (async () => {
      if (accessToken) {
        applyToken(accessToken);
        try {
          const me = await apiClient.me();
          if (!cancelled) {
            setUser(me);
            // Clears the ceiling's "could not reach sign-in" message if the
            // validation was merely slow and landed after the splash gave up.
            setError(null);
            setIsLoading(false);
          }
          return;
        } catch {
          // Access token missing/expired — fall through to a refresh attempt.
        }
      }
      if (refreshToken) {
        // Need the discovery doc to refresh; wait for it (this effect re-runs).
        if (!effectiveDiscovery || !ENTRA_CONFIG.clientId) return;
        try {
          const refreshed = await AuthSession.refreshAsync(
            { clientId: ENTRA_CONFIG.clientId, refreshToken, scopes },
            effectiveDiscovery,
          );
          if (cancelled) return;
          const bearer = USE_ID_TOKEN ? refreshed.idToken : refreshed.accessToken;
          if (!bearer) {
            throw new Error('Token refresh did not return the expected token.');
          }
          await establishSession(
            bearer,
            refreshed.refreshToken ?? refreshToken,
          );
          if (!cancelled) setIsLoading(false);
          return;
        } catch {
          // Refresh failed — fall through to sign-out.
        }
      }
      if (!cancelled) {
        resetSession();
        setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [effectiveDiscovery, applyToken, establishSession, resetSession, scopes]);

  // Handle the outcome of an interactive sign-in (code → tokens → user).
  useEffect(() => {
    if (!response) return;
    let cancelled = false;
    (async () => {
      if (response.type === 'success' && response.params.code) {
        if (!effectiveDiscovery || !request) {
          if (!cancelled) {
            setError('Sign in could not be completed. Please try again.');
            setIsSigningIn(false);
          }
          return;
        }
        try {
          const token = await AuthSession.exchangeCodeAsync(
            {
              clientId: ENTRA_CONFIG.clientId,
              code: response.params.code,
              redirectUri,
              extraParams: request.codeVerifier
                ? { code_verifier: request.codeVerifier }
                : {},
            },
            effectiveDiscovery,
          );
          if (cancelled) return;
          const bearer = USE_ID_TOKEN ? token.idToken : token.accessToken;
          if (!bearer) {
            throw new Error('Sign in did not return the expected token.');
          }
          await establishSession(bearer, token.refreshToken ?? null);
        } catch (e) {
          if (!cancelled) {
            resetSession();
            setError(errMessage(e, 'Could not complete sign in.'));
          }
        } finally {
          if (!cancelled) setIsSigningIn(false);
        }
      } else if (response.type === 'error') {
        if (!cancelled) {
          setError(response.error?.message ?? 'Sign in failed. Please try again.');
          setIsSigningIn(false);
        }
      } else {
        // 'cancel' | 'dismiss' | 'locked' — the user backed out.
        if (!cancelled) setIsSigningIn(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Only react to a new response; other deps are stable refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [response]);

  const canSignIn = authConfigured && !!request;

  const signIn = useCallback(async () => {
    setError(null);
    if (AUTH_DISABLED) return;
    if (!authConfigured) {
      setError('Single sign-on is not configured for this deployment.');
      return;
    }
    if (!request) {
      setError('Sign in is still preparing. Please try again in a moment.');
      return;
    }
    setIsSigningIn(true);
    try {
      // The outcome is handled by the response effect above; on web this opens a
      // popup and resolves there.
      await promptAsync();
    } catch (e) {
      setIsSigningIn(false);
      setError(errMessage(e, 'Could not start sign in.'));
    }
  }, [authConfigured, request, promptAsync]);

  const signOut = useCallback(() => {
    resetSession();
    setError(null);
  }, [resetSession]);

  const value = useMemo<AuthContextValue>(
    () => ({
      isAuthenticated: AUTH_DISABLED || !!user,
      isLoading,
      isSigningIn,
      authDisabled: AUTH_DISABLED,
      authConfigured,
      canSignIn,
      user,
      error,
      signIn,
      signOut,
    }),
    [user, isLoading, isSigningIn, authConfigured, canSignIn, error, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return ctx;
}

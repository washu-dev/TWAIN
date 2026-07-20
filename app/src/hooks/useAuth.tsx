import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
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
// refresh token; the API scope makes the access token's audience the TWAIN API.
const BASE_SCOPES = ['openid', 'profile', 'email', 'offline_access'];

interface AuthContextValue {
  isAuthenticated: boolean;
  // Initial validation of a stored session (blocks the app briefly on load).
  isLoading: boolean;
  // An interactive sign-in / token exchange is in flight.
  isSigningIn: boolean;
  authDisabled: boolean;
  // Whether SSO is configured (client id + API scope present).
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
    discovery,
  );

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

  // On a 401 from any request, drop the session so the guard routes to login.
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
            setIsLoading(false);
          }
          return;
        } catch {
          // Access token missing/expired — fall through to a refresh attempt.
        }
      }
      if (refreshToken) {
        // Need the discovery doc to refresh; wait for it (this effect re-runs).
        if (!discovery || !ENTRA_CONFIG.clientId) return;
        try {
          const refreshed = await AuthSession.refreshAsync(
            { clientId: ENTRA_CONFIG.clientId, refreshToken, scopes },
            discovery,
          );
          if (cancelled) return;
          await establishSession(
            refreshed.accessToken,
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
  }, [discovery, applyToken, establishSession, resetSession, scopes]);

  // Handle the outcome of an interactive sign-in (code → tokens → user).
  useEffect(() => {
    if (!response) return;
    let cancelled = false;
    (async () => {
      if (response.type === 'success' && response.params.code) {
        if (!discovery || !request) {
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
            discovery,
          );
          if (cancelled) return;
          await establishSession(token.accessToken, token.refreshToken ?? null);
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

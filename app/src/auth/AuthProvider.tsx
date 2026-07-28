/**
 * App-wide authentication provider exposing a small, platform-agnostic contract
 * via {@link useAuth}. On web it wires Microsoft Entra ID through MSAL; on native
 * (or when SSO is unconfigured) it degrades to a no-op so shared components that
 * call `useAuth()` never crash.
 *
 * SSO is web-only by design (see `src/auth/msalConfig.ts`). MSAL browser APIs are
 * only touched inside `WebAuthProvider`, which renders exclusively on web, so the
 * native bundle never constructs a `PublicClientApplication`.
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from 'react';
import { Platform } from 'react-native';
import {
  EventType,
  PublicClientApplication,
  type AccountInfo,
  type AuthenticationResult,
} from '@azure/msal-browser';
import { MsalProvider, useIsAuthenticated, useMsal } from '@azure/msal-react';
import { apiClient } from '@/api/client';
import { isAuthConfigured, loginRequest, msalConfig } from './msalConfig';

export interface AuthContextValue {
  /** False while MSAL is initializing (web only). */
  ready: boolean;
  isAuthenticated: boolean;
  account: AccountInfo | null;
  /** Redirects the browser to the WashU sign-in page. */
  login: () => Promise<void>;
  /** Redirects the browser to sign out. */
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  ready: true,
  isAuthenticated: false,
  account: null,
  login: async () => {},
  logout: async () => {},
});

/** Access the current auth state and sign-in/out actions. */
export const useAuth = (): AuthContextValue => useContext(AuthContext);

// ── Web (MSAL / Entra ID) ────────────────────────────────────────────────────

// Lazily constructed so the native bundle never instantiates it.
let instanceRef: PublicClientApplication | null = null;
const getMsalInstance = (): PublicClientApplication => {
  if (!instanceRef) instanceRef = new PublicClientApplication(msalConfig);
  return instanceRef;
};

/** Bridges MSAL React context into our {@link AuthContextValue}. */
const WebAuthBridge: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { instance, accounts } = useMsal();
  const isAuthenticated = useIsAuthenticated();
  const account = accounts[0] ?? null;

  // Feed the API client a per-request token provider so every call carries a
  // fresh Entra bearer. We send the ID token (ID-token mode: its `aud` is the
  // SPA client id, which the API validates as ENTRA_API_AUDIENCE — no exposed
  // API scope needed). acquireTokenSilent refreshes transparently when expired.
  useEffect(() => {
    if (!isAuthenticated || !account) {
      apiClient.setTokenProvider(null);
      return;
    }
    apiClient.setTokenProvider(async () => {
      try {
        const result = await instance.acquireTokenSilent({ ...loginRequest, account });
        return result.idToken;
      } catch (err) {
        console.error('[MSAL] silent token acquisition failed', err);
        return null;
      }
    });
    return () => apiClient.setTokenProvider(null);
  }, [instance, isAuthenticated, account]);

  const login = useCallback(async () => {
    await instance.loginRedirect(loginRequest);
  }, [instance]);

  const logout = useCallback(async () => {
    await instance.logoutRedirect({ account: account ?? undefined });
  }, [instance, account]);

  return (
    <AuthContext.Provider value={{ ready: true, isAuthenticated, account, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

const WebAuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [instance, setInstance] = useState<PublicClientApplication | null>(null);

  useEffect(() => {
    const msal = getMsalInstance();
    let mounted = true;
    msal
      .initialize()
      .then(() => {
        // Restore the active account after a redirect round-trip.
        const existing = msal.getAllAccounts();
        if (existing.length > 0) msal.setActiveAccount(existing[0]);
        msal.addEventCallback((event) => {
          if (event.eventType === EventType.LOGIN_SUCCESS && event.payload) {
            const { account } = event.payload as AuthenticationResult;
            if (account) msal.setActiveAccount(account);
          }
        });
      })
      .catch((err) => console.error('[MSAL] initialization failed', err))
      .finally(() => {
        if (mounted) setInstance(msal);
      });
    return () => {
      mounted = false;
    };
  }, []);

  // Brief null render while MSAL initializes and processes any redirect response.
  if (!instance) return null;

  return (
    <MsalProvider instance={instance}>
      <WebAuthBridge>{children}</WebAuthBridge>
    </MsalProvider>
  );
};

// ── Provider ─────────────────────────────────────────────────────────────────

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  if (Platform.OS === 'web' && isAuthConfigured) {
    return <WebAuthProvider>{children}</WebAuthProvider>;
  }

  // Native build, or SSO not configured: expose a no-op context with a clear error.
  const fallback: AuthContextValue = {
    ready: true,
    isAuthenticated: false,
    account: null,
    login: async () => {
      throw new Error(
        isAuthConfigured
          ? 'SSO login is only available in the web build.'
          : 'SSO is not configured. Set EXPO_PUBLIC_AZURE_TENANT_ID and ' +
            'EXPO_PUBLIC_AZURE_CLIENT_ID in app/.env, then restart the dev server.',
      );
    },
    logout: async () => {},
  };

  return <AuthContext.Provider value={fallback}>{children}</AuthContext.Provider>;
};

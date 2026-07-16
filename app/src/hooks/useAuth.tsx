import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { apiClient, AuthUser } from '@/api/client';
import { clearToken, loadToken, saveToken } from '@/api/storage';

// When EXPO_PUBLIC_AUTH_DISABLED is "true" (e.g. local dev.sh, which also runs the
// API with AUTH_DISABLED) the app skips login entirely — matching the API bypass.
const AUTH_DISABLED = process.env.EXPO_PUBLIC_AUTH_DISABLED === 'true';

interface AuthContextValue {
  isAuthenticated: boolean;
  authDisabled: boolean;
  user: AuthUser | null;
  signIn: (email: string) => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(() =>
    AUTH_DISABLED ? null : loadToken(),
  );
  const [user, setUser] = useState<AuthUser | null>(null);

  // Keep the API client's bearer token in sync with our state.
  useEffect(() => {
    apiClient.setAuthToken(token);
  }, [token]);

  // On a 401 from any request, drop the session so the guard routes to login.
  useEffect(() => {
    apiClient.setUnauthorizedHandler(() => {
      clearToken();
      setToken(null);
      setUser(null);
    });
    return () => apiClient.setUnauthorizedHandler(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      isAuthenticated: AUTH_DISABLED || !!token,
      authDisabled: AUTH_DISABLED,
      user,
      async signIn(email: string) {
        const result = await apiClient.login(email);
        saveToken(result.token);
        setToken(result.token);
        setUser(result.user);
      },
      signOut() {
        clearToken();
        setToken(null);
        setUser(null);
      },
    }),
    [token, user],
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

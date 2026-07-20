// Minimal session persistence. The MVP target is the web export, so we use
// localStorage on web and fall back to an in-memory value elsewhere (native).
//
// With Entra SSO we persist the Entra access token (sent as the API bearer) and,
// when granted, the refresh token so a returning visit can silently mint a fresh
// access token instead of forcing a new interactive sign-in.
const ACCESS_KEY = 'twain.authToken';
const REFRESH_KEY = 'twain.refreshToken';

let memory: Record<string, string | null> = {
  [ACCESS_KEY]: null,
  [REFRESH_KEY]: null,
};

const hasLocalStorage = (): boolean => typeof localStorage !== 'undefined';

function read(key: string): string | null {
  if (hasLocalStorage()) {
    try {
      return localStorage.getItem(key);
    } catch {
      return memory[key] ?? null;
    }
  }
  return memory[key] ?? null;
}

function write(key: string, value: string | null): void {
  memory[key] = value;
  if (hasLocalStorage()) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      // ignore; the in-memory copy still holds the value for this session
    }
  }
}

export interface StoredTokens {
  accessToken: string | null;
  refreshToken: string | null;
}

export function loadToken(): string | null {
  return read(ACCESS_KEY);
}

export function loadTokens(): StoredTokens {
  return { accessToken: read(ACCESS_KEY), refreshToken: read(REFRESH_KEY) };
}

export function saveTokens(tokens: {
  accessToken: string;
  refreshToken?: string | null;
}): void {
  write(ACCESS_KEY, tokens.accessToken);
  // Entra only returns a refresh token when `offline_access` is granted; keep any
  // previously stored one rather than clobbering it when this response omits it.
  if (tokens.refreshToken !== undefined) write(REFRESH_KEY, tokens.refreshToken);
}

export function clearToken(): void {
  write(ACCESS_KEY, null);
  write(REFRESH_KEY, null);
}

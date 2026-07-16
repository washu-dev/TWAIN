// Minimal session-token persistence. The MVP target is the web export, so we use
// localStorage on web and fall back to an in-memory value elsewhere (native).
const KEY = 'twain.authToken';
let memoryToken: string | null = null;

const hasLocalStorage = (): boolean => typeof localStorage !== 'undefined';

export function loadToken(): string | null {
  if (hasLocalStorage()) {
    try {
      return localStorage.getItem(KEY);
    } catch {
      return memoryToken;
    }
  }
  return memoryToken;
}

export function saveToken(token: string): void {
  memoryToken = token;
  if (hasLocalStorage()) {
    try {
      localStorage.setItem(KEY, token);
    } catch {
      // ignore; the in-memory copy still holds the token for this session
    }
  }
}

export function clearToken(): void {
  memoryToken = null;
  if (hasLocalStorage()) {
    try {
      localStorage.removeItem(KEY);
    } catch {
      // ignore
    }
  }
}

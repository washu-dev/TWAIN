import { Platform } from 'react-native';

/**
 * Remembers where a signed-out visitor was actually trying to go, so signing in
 * takes them there instead of the dashboard.
 *
 * The runner's notification emails link straight to `/conversations/<id>`. When
 * the auth guard removes that route, expo-router falls back to the `/` anchor and
 * the requested run is lost -- the researcher signs in and has to go find it. The
 * path is captured once, at module load, because that is the only moment the
 * browser's URL still holds it; MSAL's redirect flow then navigates away and back,
 * so it is kept in sessionStorage to survive the round trip.
 */
const STORAGE_KEY = 'twain.intendedPath';

// Paths that are never a destination worth returning to.
const NOT_A_DESTINATION = ['/', '/login', '/index'];

function storage(): Storage | null {
  if (Platform.OS !== 'web') return null;
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null; // privacy modes can throw on access
  }
}

/** Whether `path` is an in-app destination we would return a user to. */
function isReturnable(path: string): boolean {
  // Must be a site-relative path. Anything protocol- or host-relative could send
  // the user off-site after login, so it is refused outright.
  if (!path.startsWith('/') || path.startsWith('//')) return false;
  const [pathname] = path.split('?');
  return !NOT_A_DESTINATION.includes(pathname.replace(/\/+$/, '') || '/');
}

// Captured at import time: by the time a component renders, expo-router may
// already have replaced the URL with the anchor route.
(() => {
  const store = storage();
  if (!store) return;
  const loc = globalThis.location;
  if (!loc) return;
  const current = `${loc.pathname}${loc.search ?? ''}`;
  if (!isReturnable(current)) return;
  try {
    // Only if nothing is pending: a mid-login reload must not overwrite the
    // original target with whatever intermediate URL is showing.
    if (!store.getItem(STORAGE_KEY)) store.setItem(STORAGE_KEY, current);
  } catch {
    // Storage full or blocked -- losing the deep link is not worth an error.
  }
})();

/**
 * The remembered destination, cleared as it is read, or null.
 *
 * Read-once so a later manual visit to `/` goes to the dashboard as usual
 * instead of bouncing back to an old run forever.
 */
export function takeIntendedPath(): string | null {
  const store = storage();
  if (!store) return null;
  try {
    const path = store.getItem(STORAGE_KEY);
    store.removeItem(STORAGE_KEY);
    return path && isReturnable(path) ? path : null;
  } catch {
    return null;
  }
}

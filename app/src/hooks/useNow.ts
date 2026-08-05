import { useEffect, useState } from 'react';

/**
 * A clock that re-renders its consumer while ``active``, for live elapsed times.
 *
 * Gated rather than always-on: a screen of finished runs must not tick once per
 * second forever. One of these per screen (not per row) is the intended shape --
 * pass the value down, so ten running runs share one interval.
 *
 * The initial value is read once at mount and not refreshed in the effect body:
 * calling setState there triggers the cascading-render lint rule, and the first
 * tick corrects it within a second anyway.
 */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}

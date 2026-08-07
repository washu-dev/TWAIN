import { useEffect, useState } from 'react';
import { AccessibilityInfo } from 'react-native';

/**
 * Whether the user has asked the OS to reduce motion.
 *
 * Every animation in the app is gated on this. Not politeness: motion sickness
 * and vestibular disorders are real, "Reduce Motion" is the setting people use to
 * say so, and an interface that ignores it is unusable for them rather than
 * merely unfashionable. It is also the cheapest quality signal there is -- almost
 * nothing that looks expensive bothers to check.
 *
 * Reads once and subscribes: the setting can change while the app is open, and on
 * web it maps to the `prefers-reduced-motion` media query, which follows the OS
 * live.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    let cancelled = false;
    AccessibilityInfo.isReduceMotionEnabled().then((value) => {
      if (!cancelled) setReduced(value);
    });
    const subscription = AccessibilityInfo.addEventListener(
      'reduceMotionChanged',
      (value) => setReduced(value),
    );
    return () => {
      cancelled = true;
      subscription?.remove();
    };
  }, []);

  return reduced;
}

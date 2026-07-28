import { useEffect, useRef } from 'react';
import { Platform } from 'react-native';
import { apiClient } from '@/api/client';

// Fallback poll cadence when the SSE stream isn't available (native) or errors.
const FALLBACK_POLL_MS = 1500;

// run_events types the API emits (see runner/bridges.py PgEventSink) that change
// what the UI shows — a stage transition, a suspend for input/approval, or the
// run ending. We refetch the conversation on each so progress appears the instant
// the runner reports it, rather than on a fixed poll tick.
const REFRESH_EVENTS = [
  'run.started',
  'stage.started',
  'stage.completed',
  'run.suspended',
  'run.completed',
  'run.error',
];

/**
 * Keep a conversation live while it's active.
 *
 * On web we subscribe to the API's Server-Sent Events stream and call `onUpdate`
 * (which should refetch the conversation) on each pipeline event, so updates are
 * pushed instantly and the stream closes itself when the run ends — no constant
 * polling, and it keeps working while the tab is backgrounded. On native (no
 * `EventSource`) or if the stream errors we fall back to interval polling so the
 * behaviour degrades gracefully. `active` gates both mechanisms: nothing runs
 * once the run reaches a terminal state.
 */
export function useConversationStream(
  conversationId: string | undefined,
  active: boolean,
  onUpdate: () => void,
): void {
  // Hold the latest callback in a ref so the effect doesn't resubscribe (tearing
  // down the stream) every render when `onUpdate` is a fresh closure. The ref is
  // only read later, inside stream/poll callbacks, so syncing it in an effect
  // (rather than during render) is safe and keeps the lint rule happy.
  const onUpdateRef = useRef(onUpdate);
  useEffect(() => {
    onUpdateRef.current = onUpdate;
  }, [onUpdate]);

  useEffect(() => {
    if (!conversationId || !active) return;

    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const startPolling = () => {
      if (pollTimer) return;
      pollTimer = setInterval(() => onUpdateRef.current(), FALLBACK_POLL_MS);
    };

    const canStream = Platform.OS === 'web' && typeof EventSource !== 'undefined';
    if (!canStream) {
      startPolling();
      return () => {
        if (pollTimer) clearInterval(pollTimer);
      };
    }

    const source = new EventSource(apiClient.streamUrl(conversationId));
    const refresh = () => onUpdateRef.current();
    for (const type of REFRESH_EVENTS) source.addEventListener(type, refresh);
    // The API sends a final `done` event at a terminal state / timeout.
    source.addEventListener('done', () => {
      refresh();
      source.close();
    });
    source.onerror = () => {
      // Connection dropped, auth rejected, or the run ended: fall back to polling
      // so the view still converges even without the stream.
      source.close();
      startPolling();
    };

    return () => {
      source.close();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [conversationId, active]);
}

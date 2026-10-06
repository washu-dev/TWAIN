import { useEffect, useRef, useState } from 'react';
import { apiClient } from '@/api/client';
import { applyActivity, EMPTY_ACTIVITY, RunActivityState } from '@/utils/runActivity';

// How often the live checklist asks for new activity. The runner reports a
// Slurm job's state on its ~30 s poll (sooner on a RIS webhook), so 2 s is
// plenty while still feeling live during staging and the queue.
const ACTIVITY_POLL_MS = 2000;

/**
 * The run's in-stage activity (checklist steps + job log), kept current while
 * the run is active.
 *
 * Polls `GET /api/conversations/{id}/activity` with an id cursor, so each
 * request returns only what's new -- over the authenticated client, unlike the
 * SSE stream, which can't carry the bearer token under auth. One last fetch
 * after the run stops picks up its final steps. Failures are ignored: the next
 * tick asks again, and the screen falls back to its plain "Working…" line.
 */
export function useRunActivity(conversationId: string | undefined, active: boolean): RunActivityState {
  // Stored with the conversation it belongs to, so opening another run reads
  // as empty at once instead of flashing the previous run's checklist.
  const [store, setStore] = useState<{ id?: string; state: RunActivityState }>({
    state: EMPTY_ACTIVITY,
  });
  const cursor = useRef({ id: undefined as string | undefined, after: 0 });

  useEffect(() => {
    if (!conversationId) return;
    if (cursor.current.id !== conversationId) cursor.current = { id: conversationId, after: 0 };
    let cancelled = false;
    let inFlight = false;

    const fetchNew = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        // A long job log can span pages: drain them in one tick.
        for (let page = 0; page < 10 && !cancelled; page += 1) {
          const { data, next_after } = await apiClient.getActivity(
            conversationId, cursor.current.after);
          if (cancelled || data.length === 0) break;
          cursor.current = { id: conversationId, after: next_after };
          setStore((prev) => ({
            id: conversationId,
            state: applyActivity(prev.id === conversationId ? prev.state : EMPTY_ACTIVITY, data),
          }));
          if (data.length < 500) break;
        }
      } catch {
        // Transient; the next tick retries.
      } finally {
        inFlight = false;
      }
    };

    void fetchNew();
    if (!active) {
      return () => {
        cancelled = true;
      };
    }
    const timer = setInterval(() => void fetchNew(), ACTIVITY_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [conversationId, active]);

  return store.id === conversationId ? store.state : EMPTY_ACTIVITY;
}

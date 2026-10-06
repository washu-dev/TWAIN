import type { ActivityEvent, RunFailure, StageProgress } from '@/api/client';

// What the run is doing inside a stage, folded from `stage.progress` and
// `job.log` run events (see runner job_activity.py for the event shapes).

/** EXECUTE's checklist, in order -- also what's shown as upcoming steps. */
export const EXECUTE_STEPS = ['stage', 'preflight', 'submit', 'queue', 'run', 'fetch'] as const;

/** What an EXECUTE step is called before it has started. */
export const EXECUTE_PENDING_LABELS: Record<string, string> = {
  stage: 'Copy the run bundle to cluster storage',
  preflight: 'Check a cluster environment can run it',
  submit: 'Submit to Slurm',
  queue: 'Wait for a node',
  run: 'Run the job',
  fetch: 'Fetch the results',
};

export interface ActivityStep extends StageProgress {
  /** When this step last changed (the event's timestamp). */
  at: string;
  /** When the step first became active, for its live elapsed time. */
  startedAt: string;
}

export interface RunActivityState {
  /** Steps per stage, in the order they first appeared. */
  stages: Record<string, ActivityStep[]>;
  /** The job's recent stdout lines (bounded). */
  logLines: string[];
  logJobId: string | null;
  logSkipped: boolean;
  logTruncated: boolean;
  /** The last line hasn't seen its newline yet (pages can end mid-line). */
  logOpen: boolean;
  /** Where the run stopped and why, from its newest `run.error` (null while it runs). */
  failure: (RunFailure & { at: string }) | null;
  /** Cursor: the newest event id folded in. */
  after: number;
}

export const EMPTY_ACTIVITY: RunActivityState = {
  stages: {},
  logLines: [],
  logJobId: null,
  logSkipped: false,
  logTruncated: false,
  logOpen: false,
  failure: null,
  after: 0,
};

/** Most log lines kept in memory (the panel shows fewer). */
export const MAX_LOG_LINES = 200;

function applyStep(state: RunActivityState, p: StageProgress, at: string): RunActivityState {
  const current = state.stages[p.stage] ?? [];
  // A new EXECUTE attempt (the self-heal loop re-runs after a repair) starts
  // over at staging: clear the previous attempt's checklist and log, keeping
  // the heal step that explains why it's running again.
  const restart =
    p.stage === 'EXECUTE' && p.step === 'stage' && p.status === 'active' &&
    current.some((s) => s.step === 'stage');
  const base = restart ? current.filter((s) => s.step === 'heal') : current;
  const index = base.findIndex((s) => s.step === p.step);
  const previous = index >= 0 ? base[index] : undefined;
  const startedAt =
    previous && !(p.status === 'active' && previous.status !== 'active')
      ? previous.startedAt
      : at;
  const entry: ActivityStep = { ...p, at, startedAt };
  const steps = index >= 0
    ? base.map((s, i) => (i === index ? entry : s))
    : [...base, entry];
  return {
    ...state,
    stages: { ...state.stages, [p.stage]: steps },
    ...(restart
      ? { logLines: [], logJobId: null, logSkipped: false, logTruncated: false, logOpen: false }
      : {}),
  };
}

function applyLog(state: RunActivityState, text: string, jobId: string,
                  skipped: boolean, truncated: boolean): RunActivityState {
  const lines = [...state.logLines];
  let open = state.logOpen;
  if (skipped) {
    lines.push('… (output skipped to keep up) …');
    open = false;
  }
  // Text arrives in byte-cursor pages that may end mid-line: continue the
  // last line while it is still open instead of starting a new one.
  const parts = text.split('\n');
  if (parts[parts.length - 1] === '') parts.pop();
  if (open && lines.length && parts.length) {
    lines[lines.length - 1] += parts.shift() ?? '';
  }
  lines.push(...parts);
  return {
    ...state,
    logLines: lines.slice(-MAX_LOG_LINES),
    logJobId: jobId,
    logSkipped: state.logSkipped || skipped,
    logTruncated: state.logTruncated || truncated,
    logOpen: text ? !text.endsWith('\n') : open,
  };
}

/** Fold a page of events (oldest first) into the state; already-seen ids are ignored. */
export function applyActivity(state: RunActivityState, events: ActivityEvent[]): RunActivityState {
  let next = state;
  for (const event of events) {
    if (event.id <= next.after) continue;
    if (event.event_type === 'stage.progress') {
      next = applyStep(next, event.payload, event.created_at);
    } else if (event.event_type === 'run.error') {
      const f = event.payload.failure;
      next = f ? { ...next, failure: { ...f, at: event.created_at } } : next;
    } else if (event.event_type === 'job.log') {
      const p = event.payload;
      next = applyLog(next, p.text ?? '', p.job_id, (p.skipped_bytes ?? 0) > 0, !!p.truncated);
    }
    next = { ...next, after: event.id };
  }
  return next;
}

/** The step currently in progress for `stage` (the newest active one), if any. */
export function activeStep(state: RunActivityState, stage: string): ActivityStep | undefined {
  const steps = state.stages[stage] ?? [];
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    if (steps[i].status === 'active') return steps[i];
  }
  return undefined;
}

import React, { useEffect, useState } from 'react';
import { Animated, Platform, StyleSheet, Text, View } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';
import { useReducedMotion } from '@/hooks/useReducedMotion';

const C = Colors.light;
const USE_NATIVE_DRIVER = Platform.OS !== 'web';

/**
 * The happy-path pipeline, in order -- the stages a run can be re-run from.
 * Loop states (REPAIR, CORRECT, REPLAN) are detours off this spine, not steps.
 */
export const PIPELINE_STATES = [
  'INTAKE', 'CLARIFY', 'DECOMPOSE', 'DISCOVER', 'PLAN',
  'BUILD', 'EXECUTE', 'INTERPRET', 'VALIDATE', 'ACCEPT', 'TERMINATE',
];

/**
 * Where each loop state sits on the spine. Without it a run that ended while
 * looping (current_state REPAIR) resolves to index -1: the "Re-run from…"
 * picker disabled every row, and the old stage strip went all grey.
 */
export const LOOP_STATE_ANCHOR: Record<string, string> = {
  REPAIR: 'BUILD',
  CORRECT: 'VALIDATE',
  REPLAN: 'VALIDATE',
};

/**
 * The tracker's phases. Every pipeline state -- loops included -- belongs to
 * exactly one, so no state the runner reports can leave the tracker blank (the
 * old 11-dot strip went all grey during REPAIR, the script review in BUILD).
 */
export const TRACKER_PHASES = [
  { label: 'Plan', doing: 'Working out how to compute it',
    states: ['INTAKE', 'CLARIFY', 'DECOMPOSE', 'DISCOVER', 'PLAN'] },
  { label: 'Build', doing: 'Writing and checking the simulation script',
    states: ['BUILD', 'REPAIR'] },
  { label: 'Run on RIS', doing: 'Running on the RIS cluster',
    states: ['EXECUTE'] },
  { label: 'Check', doing: 'Reading and validating the results',
    states: ['INTERPRET', 'VALIDATE', 'CORRECT', 'REPLAN'] },
  { label: 'Results', doing: 'Writing up your results',
    states: ['ACCEPT', 'TERMINATE'] },
] as const;

/** The phase a pipeline state belongs to (-1 for an unknown state). */
export function phaseIndex(state: string): number {
  return TRACKER_PHASES.findIndex((p) => (p.states as readonly string[]).includes(state));
}

/** Statuses in which the run is not moving on its own. */
const STILL = new Set(['completed', 'error', 'cancelled', 'rejected', 'awaiting_approval', 'awaiting_input']);

interface RunTrackerProps {
  /** conversation.current_state. */
  state: string;
  /** conversation.status (omitted in the tutorial: a run in motion). */
  status?: string;
  /** What is happening right now, from the run's activity ("Submitted — Slurm job 3351710"). */
  activity?: string | null;
  /** Why it stopped, for a failed run (the failure card's headline). */
  failure?: string | null;
}

function headline(phase: (typeof TRACKER_PHASES)[number] | undefined, p: RunTrackerProps): string {
  switch (p.status) {
    case 'completed': return 'Your results are ready';
    case 'awaiting_approval': return 'Your plan is ready: review and approve it below';
    case 'awaiting_input': return 'TWAIN has a question for you below';
    case 'cancelling': return 'Terminating the run…';
    case 'cancelled': return `Terminated during ${phase?.label ?? 'the run'}`;
    case 'rejected': return 'Plan rejected';
    case 'error':
      return `Stopped during ${phase?.label ?? 'the run'}${p.failure ? `: ${p.failure}` : ''}`;
    default: return p.activity || phase?.doing || 'Starting…';
  }
}

/**
 * Where a run has got to, as a pizza tracker: five phases filled left to
 * right, the current one pulsing, and one plain sentence saying what is
 * happening in it right now.
 *
 * The tutorial shows this same component, so the explanation can't drift from
 * the thing it explains.
 */
export const RunTracker: React.FC<RunTrackerProps> = (props) => {
  const { state, status } = props;
  const reduced = useReducedMotion();
  const completed = status === 'completed';
  // The approval gate pauses the run with current_state already at BUILD, but
  // it is the plan being approved: keep Plan as the phase in progress.
  const current = completed ? TRACKER_PHASES.length - 1
    : status === 'awaiting_approval' ? phaseIndex('PLAN')
      : phaseIndex(state);
  const phase = current >= 0 ? TRACKER_PHASES[current] : undefined;
  const moving = !STILL.has(status ?? '') && current >= 0;

  // useState with a lazy initialiser, as in Motion.tsx (a ref read during render trips react-hooks/refs).
  const [pulse] = useState(() => new Animated.Value(1));
  useEffect(() => {
    if (!moving || reduced) {
      pulse.setValue(1);
      return undefined;
    }
    const loop = Animated.loop(Animated.sequence([
      Animated.timing(pulse, { toValue: 0.45, duration: 900, useNativeDriver: USE_NATIVE_DRIVER }),
      Animated.timing(pulse, { toValue: 1, duration: 900, useNativeDriver: USE_NATIVE_DRIVER }),
    ]));
    loop.start();
    return () => loop.stop();
  }, [moving, reduced, pulse]);

  const text = headline(phase, props);
  return (
    <View
      style={styles.wrap}
      accessibilityRole="progressbar"
      accessibilityValue={{ min: 0, max: TRACKER_PHASES.length, now: Math.max(0, current) + (completed ? 1 : 0), text }}
      accessibilityLiveRegion="polite"
    >
      <View style={styles.row}>
        {TRACKER_PHASES.map((p, i) => {
          const done = completed || i < current;
          const active = !completed && i === current;
          const failed = active && status === 'error';
          const stopped = active && (status === 'cancelled' || status === 'rejected');
          const fill = done ? styles.fillDone
            : failed ? styles.fillFailed
              : stopped ? styles.fillStopped
                : active ? styles.fillActive : null;
          return (
            <View key={p.label} style={styles.phase}>
              <View style={styles.track}>
                {fill && (
                  <Animated.View
                    style={[styles.fill, fill, active && moving ? { opacity: pulse } : null]}
                  />
                )}
              </View>
              <Text
                style={[styles.label, done && styles.labelDone, active && styles.labelActive]}
                numberOfLines={1}
              >
                {failed ? `✕ ${p.label}` : p.label}
              </Text>
            </View>
          );
        })}
      </View>
      <View style={styles.status}>
        <Text style={[styles.headline, status === 'error' && styles.headlineFailed]} numberOfLines={2}>
          {text}
        </Text>
        {/* The precise stage, for whoever is reading the logs alongside. */}
        {state && !completed && status !== 'awaiting_approval'
          ? <Text style={styles.stage}>{state}</Text> : null}
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  wrap: {
    backgroundColor: C.backgroundElement,
    paddingHorizontal: Spacing.three,
    paddingTop: Spacing.two,
    paddingBottom: Spacing.two,
    gap: Spacing.two,
  },
  row: { flexDirection: 'row', gap: Spacing.one },
  phase: { flex: 1, gap: Spacing.one, minWidth: 0 },
  track: { height: 8, borderRadius: 4, backgroundColor: C.borderStrong, overflow: 'hidden' },
  fill: { position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, borderRadius: 4 },
  fillDone: { backgroundColor: C.washuGreen },
  fillActive: { backgroundColor: C.washuRed },
  fillFailed: { backgroundColor: C.washuRed },
  fillStopped: { backgroundColor: C.textSecondary },
  label: { fontSize: 11, color: C.textSecondary, textAlign: 'center' },
  labelDone: { color: C.washuGreen, fontWeight: '600' },
  labelActive: { color: C.washuRed, fontWeight: '700' },
  status: { flexDirection: 'row', alignItems: 'baseline', gap: Spacing.two },
  headline: { flex: 1, fontSize: 14, fontWeight: '600', color: C.textStrong },
  headlineFailed: { color: C.washuRed },
  stage: { fontSize: 10, color: C.textSecondary, letterSpacing: 0.5 },
});

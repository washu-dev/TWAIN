import React, { useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import * as Linking from 'expo-linking';
import { Colors, Fonts, Radius, Spacing } from '@/constants/theme';
import { formatElapsed } from '@/utils/duration';
import {
  activeStep,
  ActivityStep,
  EXECUTE_PENDING_LABELS,
  EXECUTE_STEPS,
  RunActivityState,
} from '@/utils/runActivity';

const C = Colors.light;

// Lines of job output shown collapsed / expanded.
const LOG_PREVIEW_LINES = 3;
const LOG_EXPANDED_LINES = 40;

interface Props {
  /** The pipeline state the run is in (conversation.current_state). */
  stage: string;
  activity: RunActivityState;
  /** Wall-clock "now" in ms (the screen's ticking clock). */
  now: number;
  /** "4m 10s / 40m": the run's elapsed time against its whole-run budget. */
  runElapsed: string | null;
  /** Shown when nothing more specific is known ("Working…", "Terminating…"). */
  fallbackLabel: string;
  /** Show `fallbackLabel` even over an active step (terminating, awaiting input). */
  preferFallback?: boolean;
}

const seconds = (now: number, iso?: string | null) =>
  iso ? Math.max(0, (now - new Date(iso).getTime()) / 1000) : null;

/**
 * What the run is doing right now: a checklist of the current stage's steps,
 * and, while a Slurm job runs, the last lines of its output.
 *
 * Fed by `stage.progress` / `job.log` run events (useRunActivity). With none --
 * a stage that reports nothing yet, or an older runner -- it degrades to the
 * plain spinner + label + elapsed line this replaced.
 */
export function RunActivity({
  stage, activity, now, runElapsed, fallbackLabel, preferFallback = false,
}: Props) {
  const [logOpen, setLogOpen] = useState(false);
  const steps = activity.stages[stage] ?? [];
  const current = activeStep(activity, stage);

  // EXECUTE's checklist is known up front, so upcoming steps are shown too.
  const rows: (ActivityStep | { step: string; status: 'pending'; label: string })[] =
    stage === 'EXECUTE' && steps.length
      ? [
          ...steps.filter((s) => !(EXECUTE_STEPS as readonly string[]).includes(s.step)),
          ...EXECUTE_STEPS.map(
            (key) => steps.find((s) => s.step === key)
              ?? { step: key, status: 'pending' as const, label: EXECUTE_PENDING_LABELS[key] },
          ),
        ]
      : steps;

  const showLog = stage === 'EXECUTE' && activity.logLines.length > 0;
  const visibleLog = activity.logLines.slice(-(logOpen ? LOG_EXPANDED_LINES : LOG_PREVIEW_LINES));

  return (
    <View style={styles.wrap} accessibilityLiveRegion="polite">
      <View style={styles.headline}>
        <ActivityIndicator color={C.washuRed} />
        <Text style={styles.headlineText} numberOfLines={2}>
          {(!preferFallback && current?.label) || fallbackLabel}
        </Text>
        {runElapsed && (
          <Text style={styles.clock} accessibilityLabel={`Run time ${runElapsed}`}>
            {runElapsed}
            <Text style={styles.clockHint}> run budget</Text>
          </Text>
        )}
      </View>

      {rows.length > 0 && (
        <View style={styles.list}>
          {rows.map((row) => (
            <StepRow key={row.step} row={row} now={now} />
          ))}
        </View>
      )}

      {showLog && (
        <View style={styles.log}>
          <Pressable
            onPress={() => setLogOpen((open) => !open)}
            accessibilityRole="button"
            accessibilityState={{ expanded: logOpen }}
            style={styles.logHeader}
          >
            <Text style={styles.logTitle}>
              {`Job output${activity.logJobId ? ` · job ${activity.logJobId}` : ''}`}
            </Text>
            <Text style={styles.logToggle}>{logOpen ? 'Show less' : 'Show more'}</Text>
          </Pressable>
          {visibleLog.map((line, i) => (
            <Text key={i} style={styles.logLine} numberOfLines={logOpen ? undefined : 1}>
              {line || ' '}
            </Text>
          ))}
          {activity.logTruncated && (
            <Text style={styles.logNote}>
              Live output paused here to keep the page light — the full log is in the report.
            </Text>
          )}
        </View>
      )}
    </View>
  );
}

function StepRow({ row, now }: {
  row: ActivityStep | { step: string; status: 'pending'; label: string };
  now: number;
}) {
  const detail = 'detail' in row ? row.detail ?? {} : {};
  let timing: string | null = null;
  if (row.status === 'active' && 'startedAt' in row) {
    // The running job is timed against ITS Slurm limit, not the run budget.
    const ranFor = seconds(now, (detail.started_at as string) ?? row.startedAt);
    const limit = detail.time_limit_minutes as number | undefined;
    timing = ranFor !== null
      ? formatElapsed(ranFor) + (row.step === 'run' && limit ? ` of ${limit}m` : '')
      : null;
  }
  const portal = row.step === 'submit' ? (detail.portal_url as string | undefined) : undefined;
  const resources = row.step === 'submit' && row.status === 'done'
    ? [detail.partition, detail.cpus && `${detail.cpus} CPU`,
       detail.memory_mb && `${Math.round(Number(detail.memory_mb) / 1024)} GB`,
       detail.gpus && `${detail.gpus} GPU`,
       detail.time_limit_minutes && `${detail.time_limit_minutes} min`]
        .filter(Boolean).join(' · ')
    : null;

  return (
    <View style={styles.row}>
      <Text style={[styles.mark, MARK_STYLE[row.status]]}>{MARK[row.status]}</Text>
      <View style={styles.rowBody}>
        <Text style={[styles.rowLabel, row.status === 'pending' && styles.rowLabelPending]}>
          {row.label}
          {timing && <Text style={styles.rowTiming}>{`  ${timing}`}</Text>}
        </Text>
        {resources ? <Text style={styles.rowDetail}>{resources}</Text> : null}
        {portal ? (
          <Pressable
            onPress={() => void Linking.openURL(portal)}
            accessibilityRole="link"
            accessibilityLabel="Open the RIS API Portal's jobs page"
          >
            <Text style={styles.link}>Open in the RIS API Portal →</Text>
          </Pressable>
        ) : null}
      </View>
    </View>
  );
}

const MARK: Record<string, string> = { done: '✓', failed: '✕', active: '●', pending: '○' };

const styles = StyleSheet.create({
  wrap: { padding: Spacing.two, gap: Spacing.two },
  headline: { flexDirection: 'row', alignItems: 'center', gap: Spacing.two },
  headlineText: { flex: 1, color: C.textSecondary, fontSize: 13 },
  // Tabular figures so a ticking counter doesn't shuffle its own width.
  clock: { color: C.textSecondary, fontSize: 13, fontVariant: ['tabular-nums'] },
  clockHint: { color: C.textPlaceholder, fontSize: 12 },
  list: { gap: Spacing.one, paddingLeft: Spacing.one },
  row: { flexDirection: 'row', gap: Spacing.two, alignItems: 'flex-start' },
  mark: { width: 16, textAlign: 'center', fontSize: 13, lineHeight: 18 },
  rowBody: { flex: 1, gap: 2 },
  rowLabel: { color: C.textStrong, fontSize: 13, lineHeight: 18 },
  rowLabelPending: { color: C.textPlaceholder },
  rowTiming: { color: C.textSecondary, fontVariant: ['tabular-nums'] },
  rowDetail: { color: C.textSecondary, fontSize: 12 },
  link: { color: C.washuRed, fontSize: 12, fontWeight: '600' },
  log: {
    backgroundColor: C.surfaceDark,
    borderRadius: Radius.control,
    padding: Spacing.two,
    gap: 2,
  },
  logHeader: { flexDirection: 'row', justifyContent: 'space-between', marginBottom: Spacing.one },
  logTitle: { color: C.textOnDark, fontSize: 12, fontWeight: '600' },
  logToggle: { color: C.textOnDark, fontSize: 12, opacity: 0.8 },
  logLine: { color: C.textOnDark, fontFamily: Fonts?.mono, fontSize: 12, lineHeight: 16 },
  logNote: { color: C.textOnDark, fontSize: 11, opacity: 0.7, marginTop: Spacing.one },
});

const MARK_STYLE = StyleSheet.create({
  done: { color: C.washuGreen },
  failed: { color: C.washuRed },
  active: { color: C.washuRed },
  pending: { color: C.textPlaceholder },
});

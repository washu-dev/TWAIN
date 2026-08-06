import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { apiClient, ArtifactMeta, Report } from '@/api/client';
import { useAuth } from '@/hooks/useAuth';
import { useConversationStream } from '@/hooks/useConversationStream';
import { canCopy, copyText } from '@/utils/clipboard';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

// Mirrors ChatScreen/BrowseScreen. A run in any other status can still produce
// artifacts, so the report has to keep looking until it reaches one of these.
const TERMINAL_STATUSES = ['completed', 'error', 'rejected', 'cancelled'];

export const ReportScreen: React.FC = () => {
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id?: string }>();
  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    // Same auth race as ChatScreen: fetching before MSAL has a token 401s.
    if (authLoading || !isAuthenticated) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await apiClient.getReport(id as string);
        if (!cancelled) setReport(data);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load report');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, authLoading, isAuthenticated]);

  // Re-read the report while the run is still going. Separate from the initial
  // load above so it never re-triggers the spinner, and it swallows transient
  // failures because the stream will simply ask again.
  const refresh = useCallback(async () => {
    if (!id) return;
    try {
      setReport(await apiClient.getReport(id as string));
    } catch {
      // Transient; the next event or poll tick retries.
    }
  }, [id]);

  // A report opened before the run finished has to keep converging on its own.
  // ChatScreen has always been live (SSE, falling back to polling); this screen
  // fetched exactly once and then never again, so opening it mid-run pinned an
  // empty result on screen until the browser was reloaded -- while the value sat
  // in the DB the whole time. The stream stops itself at a terminal status, and
  // `error` gates it so a 401 or a 500 cannot become a poll loop.
  const stillRunning =
    !error && (!report || !TERMINAL_STATUSES.includes(report.status));
  useConversationStream(id as string | undefined, stillRunning, refresh);

  // Belt and braces for the window this screen cannot see. The runner now commits
  // artifacts before it publishes a terminal event, but a status that says
  // "finished" was previously up to 8s ahead of the last artifact, and this screen
  // stops streaming the moment it reads terminal -- so a single late write would
  // strand an empty report until a manual reload, which is the bug this whole
  // effect exists to kill. A few bounded sweeps after the run ends cost three
  // requests and remove the class of failure. Deps are the boolean and a stable
  // callback, so this runs once per run-end, never in a loop.
  const finished = !!report && TERMINAL_STATUSES.includes(report.status);
  useEffect(() => {
    if (!finished) return;
    const timers = [1500, 5000, 12000].map((ms) =>
      setTimeout(() => {
        void refresh();
      }, ms),
    );
    return () => timers.forEach(clearTimeout);
  }, [finished, refresh]);

  return (
    <SafeAreaView style={styles.container} edges={['top', 'left', 'right', 'bottom']}>
      <View style={styles.topBar}>
        {/* Direct loads (URL / refresh) have no history; fall back to home. */}
        <TouchableOpacity
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))}
          accessibilityRole="button"
        >
          <Text style={styles.back}>‹ Back</Text>
        </TouchableOpacity>
        <Text style={styles.title} numberOfLines={1}>
          {report?.conversation?.title ?? 'Report'}
        </Text>
        {/* Always reachable path back to the run's chat — from here the user
            can re-run from a step or ask for changes. Works even when the
            report was opened directly (Browse / URL), where Back can't. */}
        <TouchableOpacity
          onPress={() => router.push({ pathname: '/chat', params: { id: id as string } })}
          accessibilityRole="button"
          accessibilityLabel="Open the conversation for this run"
        >
          <Text style={styles.openChat}>Chat ›</Text>
        </TouchableOpacity>
      </View>

      {loading && <ActivityIndicator style={{ marginTop: Spacing.five }} color={C.washuRed} />}
      {error && <Text style={styles.error}>{error}</Text>}

      {report && (
        <ScrollView style={styles.scroll} contentContainerStyle={styles.content}>
          <View style={styles.badges}>
            <StatusBadge status={report.status} />
            <View style={styles.stateBadge}>
              <Text style={styles.stateBadgeText}>final state: {report.final_state}</Text>
            </View>
          </View>

          <ResultCard
            result={report.result ?? {}}
            primaryMetric={primaryMetric(report)}
            resultsDir={report.results_dir}
            fallbackOutput={report.result ? null : stdoutTail(report)}
          />

          <ValidationCard report={report} />

          <SummaryCard report={report} />

          <BudgetCard report={report} />

          <Files report={report} />
        </ScrollView>
      )}
    </SafeAreaView>
  );
};

// The metric INTERPRET (module 10) selected as the answer. Both the Result card
// and the Validation card read it through primaryMetric() so the headline and the
// verdict can never be about different numbers.
type PrimaryMetric = {
  name?: string;
  value?: number;
  uncertainty?: number;
  unit?: string;
};

function primaryMetric(report: Report | null): PrimaryMetric | null {
  const normalized =
    typeof report?.normalized_result === 'object' && report.normalized_result
      ? report.normalized_result
      : null;
  const metric = (normalized?.['primary_metric'] ?? null) as PrimaryMetric | null;
  return metric && typeof metric.value === 'number' ? metric : null;
}

// How the result was checked (Epic 6): the interpreted metric and the
// cross-validation verdict. Renders only when the run interpreted something —
// planning-only or failed runs have nothing to validate.
const ValidationCard: React.FC<{ report: Report }> = ({ report }) => {
  const validation =
    typeof report.validation === 'object' && report.validation ? report.validation : null;
  const normalized =
    typeof report.normalized_result === 'object' && report.normalized_result
      ? report.normalized_result
      : null;
  if (!validation && !normalized) return null;

  const metric = primaryMetric(report);
  const metricText =
    metric && typeof metric.value === 'number'
      ? `${metric.name} = ${formatValue(metric.value)}` +
        (metric.uncertainty ? ` ± ${formatValue(metric.uncertainty)}` : '') +
        (metric.unit ? ` ${metric.unit}` : '')
      : null;

  const status = String(validation?.['acceptance_status'] ?? 'not performed');
  const rationale =
    typeof validation?.['rationale'] === 'string' ? (validation['rationale'] as string) : null;
  const rerun = (validation?.['rerun'] ?? null) as
    | { decision?: string; stop_reason?: string; reason?: string; final_verdict?: string }
    | null;
  const stopped = rerun?.decision === 'stop';

  const statusColor =
    status === 'accepted' ? C.washuGreen : status === 'rejected' ? C.washuRed : C.warning;

  return (
    <View style={styles.card}>
      <View style={styles.validationHeader}>
        <Text style={styles.cardTitle}>Validation</Text>
        <View style={[styles.statusBadge, { backgroundColor: statusColor }]}>
          <Text style={styles.statusBadgeText}>{status.replace('_', ' ')}</Text>
        </View>
      </View>
      {metricText && <Row label="Interpreted result" value={metricText} />}
      {rationale && <Text style={styles.validationRationale}>{rationale}</Text>}
      {!validation && (
        <Text style={styles.note}>
          A result was extracted, but no reference (literature baseline or acceptance
          criterion) was available to check it against.
        </Text>
      )}
      {stopped && (
        <Text style={styles.note}>
          The automatic correction loop stopped (
          {rerun?.stop_reason ?? rerun?.reason ?? 'budget exhausted'}). The result is delivered
          for your review — verdict on record: {rerun?.final_verdict ?? status}.
        </Text>
      )}
    </View>
  );
};

const SummaryCard: React.FC<{ report: Report }> = ({ report }) => {
  const plan = typeof report.plan === 'object' && report.plan ? report.plan : null;
  // Field names follow schemas/execution_plan.schema.json.
  const method = plan?.['selected_method'] as
    | { tool_name?: string; tool_version?: string | number }
    | undefined;
  const cost = plan?.['cost_estimate'] as { min_cost?: number } | undefined;
  const compute = plan?.['compute_estimate'] as { cpu_hours?: number } | undefined;
  const exec =
    typeof report.execution_result === 'object' ? report.execution_result : null;

  const methodText = method?.tool_name
    ? `${method.tool_name} ${method.tool_version ?? ''}`.trim()
    : '—';
  const costParts = [
    cost?.min_cost != null ? `$${Number(cost.min_cost).toFixed(2)} LLM` : null,
    compute?.cpu_hours != null ? `${Number(compute.cpu_hours).toFixed(2)} CPU·h` : null,
  ].filter(Boolean);
  // Prefer the adapter's status verbatim (it distinguishes success from
  // skipped/deferred runs); fall back to the boolean for older results.
  const execText = exec
    ? String(exec['status'] ?? (exec['succeeded'] ? 'succeeded' : 'failed'))
    : 'not run locally (execution disabled)';
  // Slurm runs carry their job identity in install_log (see
  // SlurmExecutionAdapter): show which cluster/job produced the result.
  const slurmInfo = exec?.['install_log'] as
    | { job_id?: string; cluster?: string }
    | undefined;
  const slurmText = slurmInfo?.job_id
    ? `${slurmInfo.cluster ?? 'Slurm'} — job ${slurmInfo.job_id}`
    : null;

  const summary = typeof plan?.['summary'] === 'string' ? (plan['summary'] as string) : null;

  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>Summary</Text>
      {summary ? <Text style={styles.summaryText}>{summary}</Text> : null}
      <Row label="Selected method" value={methodText} />
      <Row label="Estimated cost" value={costParts.length ? costParts.join(' + ') : '—'} />
      <Row label="Execution" value={execText} />
      {slurmText && <Row label="Ran on" value={slurmText} />}
      {!plan && (
        <Text style={styles.note}>
          No execution plan was produced (the run stopped before planning). The raw specs are below.
        </Text>
      )}
    </View>
  );
};

// Actual spend for the run, from the budget.json artifact the orchestrator writes
// each step. Renders nothing until a budget snapshot exists (e.g. very early runs).
const BudgetCard: React.FC<{ report: Report }> = ({ report }) => {
  const budget = typeof report.budget === 'object' && report.budget ? report.budget : null;
  const run = budget?.run;
  if (!run) return null;

  const used = Number(run.cost ?? 0);
  const max = Number(run.max_cost ?? 0);
  const remaining = Math.max(max - used, 0);
  const pct = max > 0 ? Math.min((used / max) * 100, 100) : 0;
  const overBudget = max > 0 && used >= max;
  const mins = (secs?: number) => (secs != null ? `${(Number(secs) / 60).toFixed(1)} min` : '—');

  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>Budget</Text>
      <View style={styles.meterTrack}>
        <View
          style={[styles.meterFill, { width: `${pct}%` }, overBudget && styles.meterFillOver]}
        />
      </View>
      <Row label="LLM cost used" value={`$${used.toFixed(4)} / $${max.toFixed(2)}`} />
      <Row label="Remaining" value={`$${remaining.toFixed(4)}`} />
      <Row label="Iterations" value={`${run.iterations ?? 0} / ${run.max_iterations ?? 0}`} />
      <Row
        label="Elapsed"
        value={`${mins(run.elapsed_seconds)} / ${mins(run.wall_time_limit_seconds)}`}
      />
      {overBudget && (
        <Text style={styles.note}>This run reached its cost budget and was stopped.</Text>
      )}
    </View>
  );
};

const Row: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <View style={styles.row}>
    <Text style={styles.rowLabel}>{label}</Text>
    <Text style={styles.rowValue}>{value}</Text>
  </View>
);

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const color =
    status === 'completed' ? C.washuGreen : status === 'error' ? C.washuRed : C.textSecondary;
  return (
    <View style={[styles.statusBadge, { backgroundColor: color }]}>
      <Text style={styles.statusBadgeText}>{status}</Text>
    </View>
  );
};

const ArtifactRow: React.FC<{ conversationId: string; meta: ArtifactMeta }> = ({
  conversationId,
  meta,
}) => {
  const [expanded, setExpanded] = useState(false);
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    // The "Copied" flash outlives the row if the report reloads under it.
    if (copiedTimer.current) clearTimeout(copiedTimer.current);
  }, []);

  /** The file's text, fetching it once if this row has not loaded it yet. */
  const ensureContent = async (): Promise<string | null> => {
    if (content !== null) return content;
    setLoading(true);
    try {
      const data = await apiClient.getArtifact(conversationId, meta.name);
      setContent(data.content);
      return data.content;
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load file');
      return null;
    } finally {
      setLoading(false);
    }
  };

  const toggle = async () => {
    const next = !expanded;
    setExpanded(next);
    if (next && content === null && !loading) await ensureContent();
  };

  // Copy without needing to expand first: a researcher pasting main.py into a
  // cluster shell does not want to read it here.
  const handleCopy = async () => {
    if (loading) return;
    const text = await ensureContent();
    if (text === null) return;
    if (!(await copyText(text))) {
      setError('Could not copy to the clipboard — select the text instead.');
      return;
    }
    setCopied(true);
    if (copiedTimer.current) clearTimeout(copiedTimer.current);
    copiedTimer.current = setTimeout(() => setCopied(false), 1500);
  };

  return (
    <View style={styles.artifact}>
      {/* The toggle and the copy button are siblings rather than nested, so a tap
          on Copy cannot also expand the row. */}
      <View style={styles.artifactHeader}>
        <TouchableOpacity
          style={styles.artifactHeaderMain}
          onPress={toggle}
          accessibilityRole="button"
        >
          <Text style={styles.artifactChevron}>{expanded ? '▾' : '▸'}</Text>
          <Text style={styles.artifactName} numberOfLines={1}>{meta.name}</Text>
          <Text style={styles.artifactKind}>{meta.kind}</Text>
        </TouchableOpacity>
        {canCopy && (
          <TouchableOpacity
            style={[styles.copyBtn, loading && styles.copyBtnDisabled]}
            onPress={handleCopy}
            disabled={loading}
            accessibilityRole="button"
            accessibilityLabel={`Copy ${meta.name}`}
          >
            <Text style={styles.copyText}>{copied ? '✓ Copied' : 'Copy'}</Text>
          </TouchableOpacity>
        )}
      </View>
      {expanded && (
        <View style={styles.artifactBody}>
          {loading && <ActivityIndicator color={C.washuRed} />}
          {error && <Text style={styles.error}>{error}</Text>}
          {content !== null && (
            // Vertical scroller (capped height) wrapping a horizontal one for
            // long lines. A single horizontal ScrollView clipped anything
            // taller than the cap with no way to reach the bottom of the file.
            <ScrollView style={styles.codeScroll} nestedScrollEnabled>
              <ScrollView horizontal nestedScrollEnabled>
                <Text style={styles.code} selectable>
                  {content}
                </Text>
              </ScrollView>
            </ScrollView>
          )}
        </View>
      )}
    </View>
  );
};

function formatValue(v: unknown): string {
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(4)));
  }
  return String(v);
}

// The last lines the run's script printed — the human-readable result when no
// structured (single-line JSON) result was found in stdout.
function stdoutTail(report: Report, maxLines = 12): string | null {
  const exec = report.execution_result;
  const stdout =
    typeof exec === 'object' && exec && typeof exec['stdout'] === 'string'
      ? (exec['stdout'] as string)
      : null;
  if (!stdout) return null;
  const lines = stdout
    .split('\n')
    .map((l) => l.trimEnd())
    .filter((l) => l.trim().length > 0);
  if (lines.length === 0) return null;
  return lines.slice(-maxLines).join('\n');
}

// Headline scientific result: the property + value the run computed, plus where
// the output files are stored. Falls back gracefully for arbitrary result shapes,
// and shows the script's raw output when no structured result was printed — so
// the top of the report always answers "what did the run produce?".
const ResultCard: React.FC<{
  result: Record<string, unknown>;
  primaryMetric?: PrimaryMetric | null;
  resultsDir?: string | null;
  fallbackOutput?: string | null;
}> = ({ result, primaryMetric, resultsDir, fallbackOutput }) => {
  const propName = typeof result['property'] === 'string' ? (result['property'] as string) : null;
  // `property` is only usable as a headline when it names a key verbatim. Scripts
  // routinely print the quantity instead of the key it was stored under —
  // "standard_heat_of_formation" while the number lives in
  // standard_heat_of_formation_kJ_per_mol — and then this lookup is undefined.
  const direct = propName ? result[propName] : undefined;

  // Second choice: the metric INTERPRET chose. The agent has already decided
  // which of the printed numbers is the answer, and it is the same one validation
  // graded, so reusing it keeps the headline from ever disagreeing with the
  // verdict below — which a second, independent guess here could.
  const interpreted = typeof primaryMetric?.value === 'number' ? primaryMetric : null;

  const headlineName = direct != null ? propName : (interpreted?.name ?? null);
  const headlineValue = direct != null ? direct : interpreted?.value;
  const headlineUnit =
    direct != null ? result[`${propName}_unit`] : (interpreted?.unit ?? undefined);

  const hidden = new Set<string>(['property', 'smoke', 'output_file']);
  for (const key of [propName, headlineName]) {
    if (key) {
      hidden.add(key);
      hidden.add(`${key}_unit`);
    }
  }
  const rows = Object.entries(result).filter(
    ([k, v]) =>
      !hidden.has(k) &&
      (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'),
  );

  const hasStructured = headlineValue != null || rows.length > 0;

  return (
    <View style={styles.resultCard}>
      <Text style={styles.resultCardTitle}>Result</Text>
      {headlineName && headlineValue != null && (
        <Text style={styles.resultHeadline}>
          {headlineName}: {formatValue(headlineValue)}
          {headlineUnit ? ` ${String(headlineUnit)}` : ''}
        </Text>
      )}
      {rows.map(([k, v]) => (
        <Row key={k} label={k} value={formatValue(v)} />
      ))}
      {!hasStructured && fallbackOutput ? (
        <>
          <Text style={styles.resultFallbackLabel}>
            What the run printed (no structured result found):
          </Text>
          <Text style={styles.resultFallback} selectable>
            {fallbackOutput}
          </Text>
        </>
      ) : null}
      {!hasStructured && !fallbackOutput ? (
        <Text style={styles.resultEmpty}>
          This run has no result output (it may have stopped before executing).
          The plan, budget, and any files it did produce are below.
        </Text>
      ) : null}
      {resultsDir ? (
        <View style={styles.resultPathBox}>
          <Text style={styles.resultPathLabel}>Results stored at</Text>
          <Text style={styles.resultPath} selectable>
            {resultsDir}
          </Text>
        </View>
      ) : null}
    </View>
  );
};

// Artifacts split into what the run produced (output/…) vs. specs + the bundle.
const Files: React.FC<{ report: Report }> = ({ report }) => {
  const outputs = report.artifacts.filter((a) => a.name.startsWith('output/'));
  const details = report.artifacts.filter((a) => !a.name.startsWith('output/'));
  return (
    <>
      {outputs.length > 0 && (
        <>
          <Text style={styles.sectionHeading}>Output files</Text>
          <Text style={styles.sectionHint}>
            The files your run produced — results and solver logs.
          </Text>
          {outputs.map((a) => (
            <ArtifactRow key={a.name} conversationId={report.conversation.id} meta={a} />
          ))}
        </>
      )}

      <Text style={styles.sectionHeading}>Run details</Text>
      <Text style={styles.sectionHint}>
        Specs and the generated run bundle. run_bundle/main.py is the generated script.
      </Text>
      {report.artifacts.length === 0 && (
        <Text style={styles.empty}>No files were produced for this run yet.</Text>
      )}
      {details.map((a) => (
        <ArtifactRow key={a.name} conversationId={report.conversation.id} meta={a} />
      ))}
    </>
  );
};

const mono = Platform.select({ ios: 'Courier', android: 'monospace', default: 'monospace' });

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: C.background },
  topBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    backgroundColor: C.washuRed,
  },
  back: { color: C.washuWhite, fontSize: 16, fontWeight: '600', width: 48 },
  openChat: { color: C.washuWhite, fontSize: 15, fontWeight: '600', width: 48, textAlign: 'right' },
  title: { color: C.washuWhite, fontSize: 16, fontWeight: '700', flex: 1, textAlign: 'center' },
  scroll: { flex: 1 },
  content: { padding: Spacing.three, gap: Spacing.three },
  badges: { flexDirection: 'row', gap: Spacing.two, alignItems: 'center' },
  statusBadge: { borderRadius: 12, paddingHorizontal: Spacing.three, paddingVertical: 4 },
  statusBadgeText: { color: C.washuWhite, fontWeight: '700', fontSize: 12 },
  stateBadge: {
    borderRadius: 12,
    paddingHorizontal: Spacing.three,
    paddingVertical: 4,
    backgroundColor: C.backgroundElement,
  },
  stateBadgeText: { color: C.textSecondary, fontSize: 12, fontWeight: '600' },
  card: {
    borderRadius: 12,
    borderWidth: 1,
    borderColor: C.backgroundElement,
    padding: Spacing.three,
    gap: Spacing.one,
  },
  cardTitle: { fontSize: 16, fontWeight: '700', color: C.text, marginBottom: Spacing.one },
  meterTrack: {
    height: 8,
    borderRadius: 4,
    backgroundColor: C.backgroundElement,
    overflow: 'hidden',
    marginBottom: Spacing.two,
  },
  meterFill: { height: 8, borderRadius: 4, backgroundColor: C.washuGreen },
  meterFillOver: { backgroundColor: C.washuRed },
  summaryText: { fontSize: 14, color: C.text, lineHeight: 20, marginBottom: Spacing.two },
  validationHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: Spacing.one,
  },
  validationRationale: { fontSize: 13, color: C.text, lineHeight: 19, marginTop: Spacing.one },
  resultCard: {
    borderRadius: 12,
    borderWidth: 1,
    borderColor: C.washuGreen,
    borderLeftWidth: 4,
    padding: Spacing.three,
    gap: Spacing.one,
    backgroundColor: C.washuWhite,
  },
  resultCardTitle: {
    fontSize: 12,
    fontWeight: '700',
    color: C.washuGreen,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  resultHeadline: { fontSize: 22, fontWeight: '700', color: C.text, marginVertical: Spacing.one },
  resultFallbackLabel: { fontSize: 12, color: C.textSecondary, marginTop: Spacing.one },
  resultFallback: { fontFamily: mono, fontSize: 12, color: C.text, lineHeight: 17 },
  resultEmpty: { fontSize: 13, color: C.textSecondary, fontStyle: 'italic' },
  resultPathBox: { marginTop: Spacing.two, gap: 2 },
  resultPathLabel: {
    fontSize: 11,
    color: C.textSecondary,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  resultPath: { fontSize: 12, color: C.text, fontFamily: mono },
  row: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 3, gap: Spacing.three },
  rowLabel: { color: C.textSecondary, fontSize: 14 },
  rowValue: { color: C.text, fontSize: 14, fontWeight: '600', flexShrink: 1, textAlign: 'right' },
  note: { color: C.textSecondary, fontSize: 13, marginTop: Spacing.two, fontStyle: 'italic' },
  sectionHeading: { fontSize: 18, fontWeight: '700', color: C.text, marginTop: Spacing.two },
  sectionHint: { color: C.textSecondary, fontSize: 13, marginTop: -Spacing.two },
  empty: { color: C.textSecondary, fontSize: 14, fontStyle: 'italic' },
  artifact: { borderRadius: 10, borderWidth: 1, borderColor: C.backgroundElement, overflow: 'hidden' },
  artifactHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    padding: Spacing.three,
    backgroundColor: C.backgroundElement,
  },
  artifactHeaderMain: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    minWidth: 0,          // lets the long file name ellipsize instead of pushing Copy out
  },
  copyBtn: {
    paddingVertical: Spacing.one,
    paddingHorizontal: Spacing.two,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: C.textSecondary,
    backgroundColor: C.washuWhite,
  },
  copyBtnDisabled: { opacity: 0.5 },
  copyText: { fontSize: 11, fontWeight: '700', color: C.text },
  artifactChevron: { fontSize: 14, color: C.washuRed, width: 16 },
  artifactName: { flex: 1, fontSize: 14, fontWeight: '600', color: C.text },
  artifactKind: { fontSize: 11, color: C.textSecondary, textTransform: 'uppercase' },
  artifactBody: { padding: Spacing.three, backgroundColor: C.washuWhite },
  codeScroll: { maxHeight: 480 },
  code: { fontFamily: mono, fontSize: 12, color: C.text },
  error: { color: C.washuRed, padding: Spacing.three },
});

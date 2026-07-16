import React, { useEffect, useState } from 'react';
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
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

export const ReportScreen: React.FC = () => {
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id?: string }>();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
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
  }, [id]);

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
        <View style={{ width: 48 }} />
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

          {(report.result || report.results_dir) && (
            <ResultCard result={report.result ?? {}} resultsDir={report.results_dir} />
          )}

          <SummaryCard report={report} />

          <Files report={report} />
        </ScrollView>
      )}
    </SafeAreaView>
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

  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>Summary</Text>
      <Row label="Selected method" value={methodText} />
      <Row label="Estimated cost" value={costParts.length ? costParts.join(' + ') : '—'} />
      <Row label="Execution" value={execText} />
      {!plan && (
        <Text style={styles.note}>
          No execution plan was produced (the run stopped before planning). The raw specs are below.
        </Text>
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

  const toggle = async () => {
    const next = !expanded;
    setExpanded(next);
    if (next && content === null && !loading) {
      setLoading(true);
      try {
        const data = await apiClient.getArtifact(conversationId, meta.name);
        setContent(data.content);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load file');
      } finally {
        setLoading(false);
      }
    }
  };

  return (
    <View style={styles.artifact}>
      <TouchableOpacity style={styles.artifactHeader} onPress={toggle} accessibilityRole="button">
        <Text style={styles.artifactChevron}>{expanded ? '▾' : '▸'}</Text>
        <Text style={styles.artifactName} numberOfLines={1}>{meta.name}</Text>
        <Text style={styles.artifactKind}>{meta.kind}</Text>
      </TouchableOpacity>
      {expanded && (
        <View style={styles.artifactBody}>
          {loading && <ActivityIndicator color={C.washuRed} />}
          {error && <Text style={styles.error}>{error}</Text>}
          {content !== null && (
            <ScrollView horizontal style={styles.codeScroll}>
              <Text style={styles.code}>{content}</Text>
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

// Headline scientific result: the property + value the run computed, plus where
// the output files are stored. Falls back gracefully for arbitrary result shapes.
const ResultCard: React.FC<{ result: Record<string, unknown>; resultsDir?: string | null }> = ({
  result,
  resultsDir,
}) => {
  const propName = typeof result['property'] === 'string' ? (result['property'] as string) : null;
  const headline = propName ? result[propName] : undefined;
  const unit = propName ? result[`${propName}_unit`] : undefined;

  const hidden = new Set<string>(['property', 'smoke', 'output_file']);
  if (propName) {
    hidden.add(propName);
    hidden.add(`${propName}_unit`);
  }
  const rows = Object.entries(result).filter(
    ([k, v]) =>
      !hidden.has(k) &&
      (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'),
  );

  return (
    <View style={styles.resultCard}>
      <Text style={styles.resultCardTitle}>Result</Text>
      {propName && headline != null && (
        <Text style={styles.resultHeadline}>
          {propName}: {formatValue(headline)}
          {unit ? ` ${String(unit)}` : ''}
        </Text>
      )}
      {rows.map(([k, v]) => (
        <Row key={k} label={k} value={formatValue(v)} />
      ))}
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
  back: { color: '#FFFFFF', fontSize: 16, fontWeight: '600', width: 48 },
  title: { color: '#FFFFFF', fontSize: 16, fontWeight: '700', flex: 1, textAlign: 'center' },
  scroll: { flex: 1 },
  content: { padding: Spacing.three, gap: Spacing.three },
  badges: { flexDirection: 'row', gap: Spacing.two, alignItems: 'center' },
  statusBadge: { borderRadius: 12, paddingHorizontal: Spacing.three, paddingVertical: 4 },
  statusBadgeText: { color: '#FFFFFF', fontWeight: '700', fontSize: 12 },
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
  artifactChevron: { fontSize: 14, color: C.washuRed, width: 16 },
  artifactName: { flex: 1, fontSize: 14, fontWeight: '600', color: C.text },
  artifactKind: { fontSize: 11, color: C.textSecondary, textTransform: 'uppercase' },
  artifactBody: { padding: Spacing.three, backgroundColor: C.washuWhite },
  codeScroll: { maxHeight: 320 },
  code: { fontFamily: mono, fontSize: 12, color: C.text },
  error: { color: C.washuRed, padding: Spacing.three },
});

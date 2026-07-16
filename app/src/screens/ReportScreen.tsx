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

          <SummaryCard report={report} />

          <Text style={styles.sectionHeading}>Files</Text>
          <Text style={styles.sectionHint}>
            Tap to expand. {`run_bundle/main.py`} is the generated pymatgen script.
          </Text>
          {report.artifacts.length === 0 && (
            <Text style={styles.empty}>No files were produced for this run yet.</Text>
          )}
          {report.artifacts.map((a) => (
            <ArtifactRow key={a.name} conversationId={report.conversation.id} meta={a} />
          ))}
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

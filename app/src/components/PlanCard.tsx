import React from 'react';
import { View, Text, StyleSheet, Platform } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';
import { formatDurationHours } from '@/utils/duration';

const C = Colors.light;

/**
 * The plan the runner publishes at the approval gate, as the app reads it.
 *
 * Every field is optional because this is a snapshot of a plan artifact, not a
 * contract the app controls: a plan missing `cost_estimate` must render, not
 * crash. The card simply omits what is absent.
 */
export type PlanSummary = {
  compute_target?: string;
  slurm_cluster?: string;
  summary?: string | null;
  goal_id?: string | null;
  target_system?: {
    formula?: string;
    kind?: string;
    crystal?: { name?: string; phase?: string };
  } | null;
  requested_property?: string | null;
  selected_method?: {
    tool_name?: string;
    tool_version?: string | number;
    calculator?: string;
    libraries?: string[];
  } | null;
  cost_estimate?: { min_cost?: number } | null;
  compute_estimate?: { cpu_hours?: number } | null;
  slurm_request?: {
    cpu_count?: number;
    gpu_count?: number;
    ram?: number;
    max_time?: number;
  };
  // Per-node ceilings of the cluster (same units as slurm_request), shown on
  // the editable fields and used to clamp what the user can request.
  slurm_limits?: {
    cpu_count?: number;
    gpu_count?: number;
    ram?: number;
    max_time?: number;
  } | null;
  // Why each suggested figure is what it is, keyed by slurm_request field.
  slurm_rationale?: Record<string, string> | null;
  // Whose ceilings slurm_limits are ("compute2 node" / "this machine").
  limits_source?: string | null;
  acceptance_metrics?: { metric_name?: string; target_value?: number; tolerance?: number }[] | null;
  safety_notes?: string[] | null;
  note?: string;
};

export function parsePlanSummary(content: string): PlanSummary | null {
  try {
    const parsed = JSON.parse(content);
    return typeof parsed === 'object' && parsed ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Renders the approval-gate plan: the plain-language summary first, then the
 * concrete method / system / cost / resources.
 *
 * Takes the raw artifact string rather than a parsed object so it can render an
 * unparseable one verbatim instead of showing nothing -- and so the tutorial can
 * explain the card by passing a realistic plan and displaying the genuine
 * component. There is one definition, so the walkthrough cannot fall behind it.
 */
export const PlanCard: React.FC<{ content: string }> = ({ content }) => {
  const plan = parsePlanSummary(content);
  if (!plan) {
    return (
      <View style={styles.planCard}>
        <Text style={styles.planTitle}>Proposed execution plan</Text>
        <Text style={styles.planBody}>{content}</Text>
      </View>
    );
  }

  const method = plan.selected_method ?? undefined;
  const methodText = method?.tool_name
    ? [
        `${method.tool_name}${method.tool_version ? ` ${method.tool_version}` : ''}`,
        method.calculator ? `+ ${method.calculator}` : '',
      ]
        .filter(Boolean)
        .join(' ')
    : undefined;
  const libs = method?.libraries?.length ? method.libraries.join(' + ') : undefined;

  const sys = plan.target_system ?? undefined;
  const sysText = sys
    ? [sys.formula ?? sys.crystal?.name, sys.crystal?.phase, sys.kind].filter(Boolean).join(', ')
    : undefined;

  const cost = plan.cost_estimate?.min_cost;
  const cpu = plan.compute_estimate?.cpu_hours;
  const costText = [
    cost != null ? `$${Number(cost).toFixed(2)} LLM` : null,
    cpu != null ? `${Number(cpu).toFixed(2)} CPU·h` : null,
  ]
    .filter(Boolean)
    .join(' + ');

  const metrics = plan.acceptance_metrics ?? [];
  const notes = plan.safety_notes ?? [];
  const slurm = plan.slurm_request;

  return (
    <View style={styles.planCard}>
      <Text style={styles.planTitle}>Proposed execution plan</Text>
      {plan.compute_target === 'slurm' && (
        <Text style={styles.planTarget}>
          Will submit to RIS / Slurm
          {plan.slurm_cluster ? ` (${plan.slurm_cluster})` : ''}
        </Text>
      )}
      {plan.summary ? <Text style={styles.planSummary}>{plan.summary}</Text> : null}
      {sysText ? <PlanRow label="System" value={sysText} /> : null}
      {plan.requested_property ? (
        <PlanRow label="Property" value={plan.requested_property} />
      ) : null}
      {methodText ? (
        <PlanRow label="Method" value={libs ? `${methodText}  ·  ${libs}` : methodText} />
      ) : null}
      {costText ? <PlanRow label="Estimated cost" value={costText} /> : null}
      {slurm ? (
        <PlanRow
          label="Slurm ask"
          value={`${slurm.cpu_count ?? '—'} CPU, ${slurm.gpu_count ?? 0} GPU, ${
            slurm.ram ?? '—'
          } GB RAM, ${
            (slurm.max_time != null && formatDurationHours(slurm.max_time)) || '—'
          } wall`}
        />
      ) : null}
      {plan.goal_id ? <PlanRow label="Goal" value={plan.goal_id} /> : null}
      {metrics.length > 0 ? (
        <PlanRow
          label="Accept if"
          value={metrics
            .map((m) => `${m.metric_name} ≈ ${m.target_value} ± ${m.tolerance}`)
            .join('; ')}
        />
      ) : null}
      {notes.length > 0 ? (
        <View style={styles.planNotes}>
          <Text style={styles.planNotesLabel}>Notes</Text>
          {notes.map((n, i) => (
            <Text key={`note-${i}`} style={styles.planNote}>
              • {n}
            </Text>
          ))}
        </View>
      ) : null}
    </View>
  );
};

const PlanRow: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <View style={styles.planRow}>
    <Text style={styles.planRowLabel}>{label}</Text>
    <Text style={styles.planRowValue}>{value}</Text>
  </View>
);

const styles = StyleSheet.create({
  planCard: {
    alignSelf: 'stretch',
    borderRadius: 12,
    borderWidth: 1,
    borderColor: C.washuGreen,
    padding: Spacing.three,
    backgroundColor: C.washuWhite,
  },
  planTitle: { fontSize: 14, fontWeight: '700', color: C.washuGreen, marginBottom: Spacing.two },
  planTarget: { fontSize: 13, fontWeight: '700', color: C.washuRed, marginBottom: Spacing.one },
  planBody: {
    fontSize: 12,
    color: C.text,
    fontFamily: Platform.select({ ios: 'Courier', android: 'monospace', default: 'monospace' }),
  },
  planSummary: { fontSize: 14, color: C.text, lineHeight: 20, marginBottom: Spacing.two },
  planRow: { flexDirection: 'row', gap: Spacing.two, paddingVertical: 3 },
  planRowLabel: { fontSize: 12, color: C.textSecondary, fontWeight: '600', width: 96 },
  planRowValue: { fontSize: 13, color: C.text, flex: 1 },
  planNotes: { marginTop: Spacing.two, gap: 3 },
  planNotesLabel: {
    fontSize: 11,
    color: C.washuGreen,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  planNote: { fontSize: 12, color: C.textSecondary, lineHeight: 17 },
});

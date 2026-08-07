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

/** The resource figures a researcher approved, when they changed TWAIN's. */
export type ApprovedResources = {
  cpu_count?: number;
  gpu_count?: number;
  ram?: number;
  max_time?: number;
};

/**
 * Renders the approval-gate plan: the plain-language summary first, then the
 * concrete method / system / cost / resources.
 *
 * Takes the raw artifact string rather than a parsed object so it can render an
 * unparseable one verbatim instead of showing nothing -- and so the tutorial can
 * explain the card by passing a realistic plan and displaying the genuine
 * component. There is one definition, so the walkthrough cannot fall behind it.
 *
 * ``approved`` is what the researcher actually submitted, when they edited the
 * suggestion. The card is rendered from the approval_request message, which is an
 * immutable record of what was PROPOSED -- so a run approved with 10 minutes went
 * on displaying TWAIN's suggested "4h wall" forever, and reading the transcript
 * afterwards told you the run had used 4 hours. It had not: the job got
 * --time=00:10:00 (run 913c1ee9). Showing the approved figure, and marking it as
 * yours, is the difference between a record and a misleading one.
 */
/** An acceptance criterion as the researcher submitted it. */
export type ApprovedMetric = {
  metric_name?: string;
  target_value?: number | null;
  tolerance?: number | null;
};

export const PlanCard: React.FC<{
  content: string;
  approved?: ApprovedResources | null;
  approvedMetrics?: ApprovedMetric[] | null;
}> = ({ content, approved, approvedMetrics }) => {
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

  // The approved bar wins over the proposed one, for the same reason the approved
  // resources do: this card is the record of a run, and the run used yours.
  const proposedMetrics = plan.acceptance_metrics ?? [];
  const metrics = approvedMetrics?.length ? approvedMetrics : proposedMetrics;
  const metricsAmended = !!approvedMetrics?.length
    && JSON.stringify(approvedMetrics.map(describeMetric))
       !== JSON.stringify(proposedMetrics.map(describeMetric));
  const notes = plan.safety_notes ?? [];
  const proposed = plan.slurm_request;
  // Field by field: a researcher who changed only the wall time should not see
  // the CPU count relabelled as theirs.
  const slurm = proposed || approved ? { ...proposed, ...(approved ?? {}) } : undefined;
  const changed = (key: keyof ApprovedResources) => {
    const mine = approved?.[key];
    const theirs = proposed?.[key];
    if (mine == null || theirs == null) return false;
    const a = Number(mine);
    const b = Number(theirs);
    if (!Number.isFinite(a) || !Number.isFinite(b)) return mine !== theirs;
    // Half a step, not exact inequality -- see EDIT_STEP.
    return Math.abs(a - b) > (EDIT_STEP[key] ?? 1) / 2;
  };
  const amended = slurm
    ? (['cpu_count', 'gpu_count', 'ram', 'max_time'] as (keyof ApprovedResources)[])
        .filter(changed)
    : [];

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
          label={amended.length ? 'Slurm ask (yours)' : 'Slurm ask'}
          value={`${slurm.cpu_count ?? '—'} CPU, ${slurm.gpu_count ?? 0} GPU, ${
            slurm.ram ?? '—'
          } GB RAM, ${
            (slurm.max_time != null && formatDurationHours(slurm.max_time)) || '—'
          } wall`}
        />
      ) : null}
      {amended.length > 0 ? (
        <Text style={styles.amendedNote}>
          {`You changed ${amended.map(FIELD_LABELS).join(', ')} before approving. `
            + `TWAIN suggested ${
              (proposed?.max_time != null && formatDurationHours(proposed.max_time)) || '—'
            } wall, ${proposed?.cpu_count ?? '—'} CPU, ${proposed?.ram ?? '—'} GB RAM.`}
        </Text>
      ) : null}
      {plan.goal_id ? <PlanRow label="Goal" value={plan.goal_id} /> : null}
      {metrics.length > 0 ? (
        <PlanRow
          label={metricsAmended ? 'Accept if (yours)' : 'Accept if'}
          value={metrics.map(describeMetric).join('; ')}
        />
      ) : null}
      {metricsAmended ? (
        <Text style={styles.amendedNote}>
          {`You set this bar before approving. TWAIN proposed `
            + `${proposedMetrics.map(describeMetric).join('; ') || 'none'}.`}
        </Text>
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

/**
 * One acceptance criterion in words.
 *
 * A null target is common and meaningful -- TWAIN writes it when it has no
 * defensible expected value -- but it rendered as the literal string
 * "bandgap ≈ null ± null", which reads as a bug rather than as "nothing to check
 * this against". Say the latter, since it is what actually happens at VALIDATE.
 */
function describeMetric(m: {
  metric_name?: string;
  target_value?: number | null;
  tolerance?: number | null;
}): string {
  const name = m.metric_name ?? 'metric';
  if (m.target_value == null) return `${name} — no target set`;
  if (m.tolerance == null) return `${name} ≈ ${m.target_value}`;
  return `${name} ≈ ${m.target_value} ± ${m.tolerance}`;
}

const FIELD_LABELS = (key: keyof ApprovedResources): string =>
  ({ cpu_count: 'CPUs', gpu_count: 'GPUs', ram: 'RAM', max_time: 'wall time' })[key];

/**
 * The precision each field can actually be EDITED at, in the field's own unit.
 *
 * A difference smaller than half a step cannot be something a researcher
 * expressed, because the editor gives them no way to express it -- so it is
 * arithmetic drift, not an amendment. Wall time is entered as a whole number of
 * minutes, so its step is one minute; the rest are whole counts.
 *
 * Comparing the raw numbers with `!==` instead reported the app's OWN conversion
 * as the researcher's edit. A plan proposing a 0.17h cap seeds the editor as
 * "10 min" and submits as 10/60 = 0.16666..., which is not 0.17 -- so an
 * untouched approval relabelled the whole Slurm ask "(yours)" and claimed a
 * change nobody made. The ceiling clamps and the RAM floor did the same.
 *
 * Half a step rather than a full one: rounding hours to whole minutes moves a
 * value by at most half a minute, so half a step is exactly the line between
 * "the editor rounded this" and "someone typed a different number".
 */
const EDIT_STEP: Partial<Record<keyof ApprovedResources, number>> = {
  max_time: 1 / 60,
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
  amendedNote: {
    fontSize: 12,
    color: C.washuRed,
    lineHeight: 17,
    marginTop: Spacing.one,
  },
});

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
  Modal,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { apiClient, Conversation, Message, RunIssue } from '@/api/client';
import { IssueModal } from '@/components/IssueModal';
import { ReportIssueModal } from '@/components/ReportIssueModal';
import { useAuth } from '@/hooks/useAuth';
import { useNow } from '@/hooks/useNow';
import { formatDurationHours, formatElapsed, parseDurationHours } from '@/utils/duration';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

// Exact lead-in the planner puts on the safety note when the best-fit engine
// can't run on the cluster (see ENGINE_UNAVAILABLE_PREFIX in
// modules/16_agent_mesh_control_plane/statemachine.py — keep in sync). The
// approval card keys off it to offer a one-tap GitHub provisioning request.
const ENGINE_UNAVAILABLE_PREFIX = 'ENGINE UNAVAILABLE ON THIS DEPLOYMENT: ';

// The happy-path pipeline states shown in the stepper (loops CORRECT/REPLAN omitted).
const PIPELINE_STATES = [
  'INTAKE', 'CLARIFY', 'DECOMPOSE', 'DISCOVER', 'PLAN',
  'BUILD', 'EXECUTE', 'INTERPRET', 'VALIDATE', 'ACCEPT', 'TERMINATE',
];

// Where the off-spine loop states sit on the happy path. Without this, a run
// that ended while looping (e.g. current_state REPAIR) resolved to index -1 and
// every row of the "Re-run from…" picker was disabled.
const LOOP_STATE_ANCHOR: Record<string, string> = {
  REPAIR: 'BUILD',
  CORRECT: 'VALIDATE',
  REPLAN: 'VALIDATE',
};

// Stages a finished run can be restarted from, with plain-language descriptions
// of what re-running each one redoes. A re-run resets the chosen stage and every
// stage after it, keeping the earlier work as input.
/** An in-progress "re-run from here, with these changes" draft. */
type RerunDraft = {
  state: string;
  label: string;
  /** Free text folded into the intent. Offered for every stage. */
  note: string;
  /** The opening prompt, for INTAKE only; null when the stage doesn't re-read it. */
  request: string | null;
  /** Resource request, for stages after PLAN only; null when a fresh plan would
      overwrite it (the API rejects it there too). */
  slurm: SlurmDraft | null;
};

// Stages that run after plan synthesis, so the plan on disk survives the rewind
// and an edited resource request still means something. Mirrors the API's own
// check, which is the authority.
const AFTER_PLAN_STAGES = ['BUILD', 'EXECUTE', 'INTERPRET', 'VALIDATE', 'ACCEPT'];

const RERUN_STAGES: { state: string; label: string; desc: string }[] = [
  { state: 'INTAKE', label: 'Intake', desc: 'Re-read your request from scratch' },
  { state: 'CLARIFY', label: 'Clarify', desc: 'Re-ask the clarifying questions' },
  { state: 'DECOMPOSE', label: 'Decompose', desc: 'Rebuild the goal breakdown' },
  { state: 'DISCOVER', label: 'Discover', desc: 'Re-pick the candidate tools' },
  { state: 'PLAN', label: 'Plan', desc: 'Re-synthesize the execution plan' },
  { state: 'BUILD', label: 'Build', desc: 'Regenerate the run code' },
  { state: 'EXECUTE', label: 'Execute', desc: 'Re-run the calculation' },
];

const ACTIVE_STATUSES = ['running', 'awaiting_input', 'awaiting_approval', 'cancelling'];
const TERMINAL_STATUSES = ['completed', 'error', 'rejected', 'cancelled'];
const POLL_MS = 1500;
const MIN_RAM_GB = 4;

type SlurmDraft = {
  cpu_count: string;
  gpu_count: string;
  ram: string;
  max_time: string;
};

type PlanSummary = {
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

function parsePlanSummary(content: string): PlanSummary | null {
  try {
    const parsed = JSON.parse(content);
    return typeof parsed === 'object' && parsed ? parsed : null;
  } catch {
    return null;
  }
}

export const ChatScreen: React.FC = () => {
  const router = useRouter();
  const params = useLocalSearchParams<{ id?: string }>();
  // isLoading/isAuthenticated gate the initial fetch: the request interceptor
  // sends a call with no Authorization header when MSAL has no token yet, and the
  // API answers 401 -- so a cold load raced auth and surfaced an error to a user
  // who was perfectly entitled to the run.
  const { user, isAuthenticated, isLoading: authLoading } = useAuth();
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [input, setInput] = useState('');
  const [issueOpen, setIssueOpen] = useState(false); // "provision this engine" GitHub issue
  // The researcher's edits to the Slurm resource request, keyed to the approval
  // card they were made on so a fresh card reseeds from its own plan.
  const [slurmEdit, setSlurmEdit] = useState<{ key: string; draft: SlurmDraft } | null>(null);
  const [budget, setBudget] = useState('');  // per-run cost cap (USD); blank => default
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Reporting a problem with *this* run. Available for as long as the run window
  // is open — mid-run (when a stall or a wrong plan is what you want to report)
  // and after it ends alike.
  const [reporting, setReporting] = useState(false);
  const [reportedIssues, setReportedIssues] = useState<RunIssue[]>([]);
  const [rerunOpen, setRerunOpen] = useState(false);  // "Re-run from…" picker
  // Re-running from Intake re-reads the opening request, so it is offered for
  // editing first; null means the picker is showing its stage list.
  // What to change on a re-run, for whichever stage was picked. Every stage gets
  // a note ("what should be different?"), which the runner folds into the intent
  // so the re-planned stages actually see it. Two stages also get structured
  // fields, because they have an editable representation: INTAKE re-reads the
  // opening prompt, and anything after PLAN re-uses the existing plan and so can
  // take an edited resource request. Previously only INTAKE offered an editor and
  // every other stage was a bare "run it again", with no way to say why.
  const [rerunDraft, setRerunDraft] = useState<RerunDraft | null>(null);
  const scrollRef = useRef<ScrollView>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const conversationId = conversation?.id ?? (params.id as string | undefined);
  const status = conversation?.status;
  const isActive = !!status && ACTIVE_STATUSES.includes(status);
  const isTerminal = !!status && TERMINAL_STATUSES.includes(status);
  // One clock, ticking only while the run is active (see useNow).
  const now = useNow(isActive);
  const terminalMessage =
    status === 'completed'
      ? '✓ Simulation complete — your results are ready.'
      : status === 'rejected'
        ? 'Plan rejected — nothing was executed.'
        : status === 'cancelled'
          ? 'Run terminated.'
          : 'The run ended with an error.';
  const cancelling = status === 'cancelling';

  const messages = conversation?.messages ?? [];
  const awaitingApproval = status === 'awaiting_approval';
  // The prompt that started the run, prefilled when re-running from Intake.
  const openingRequest =
    messages.find((m) => m.role === 'user' && m.kind === 'chat')?.content ?? '';

  // Each gate that can ask something posts its own message kind (see
  // runner/bridges.py), so the buttons key off identity rather than sniffing the
  // prompt text. Answers go through the same reply channel typing would, so the
  // composer stays a valid fallback — including for conversations started before
  // the kinds existed, whose questions are plain 'clarification'.
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
  const pendingGate = status === 'awaiting_input' ? lastAssistant?.kind : undefined;
  const yesNoPending = pendingGate === 'heavy_confirm';
  const acceptOrRerunPending = pendingGate === 'validation_gate';
  const approvalContent =
    [...messages].reverse().find((m) => m.kind === 'approval_request')?.content ?? null;
  const approvalPlan = approvalContent ? parsePlanSummary(approvalContent) : null;

  // Planner flagged that the best-fit engine can't run on the cluster: surface
  // it beside the approval buttons with a prefilled "please provision it"
  // GitHub issue, so the researcher decides (approve the substitute / request
  // the engine) instead of getting a silent substitution.
  const engineNote =
    (approvalPlan?.safety_notes ?? []).find((n) => n.startsWith(ENGINE_UNAVAILABLE_PREFIX)) ??
    null;
  const blockedEngines = engineNote
    ? engineNote.slice(ENGINE_UNAVAILABLE_PREFIX.length).split(' would fit')[0]
    : null;
  const engineIssueBody = engineNote
    ? [
        'While planning a run, TWAIN reported:',
        '',
        engineNote,
        '',
        `Please provision ${blockedEngines} on the RIS cluster: add an env spec under ` +
          'scripts/ris/envs/ and run scripts/ris/provision_envs.sh.',
        '',
        `Conversation: ${conversationId ?? 'n/a'}`,
      ].join('\n')
    : undefined;

  const slurmLimits = approvalPlan?.slurm_limits ?? null;
  const slurmRationale = approvalPlan?.slurm_rationale ?? null;
  // "12m 04s / 4h" -- how long this run has been going, against the wall-time cap
  // it was approved with. started_at is the newest run.started, so a resumed run
  // times its current slice rather than reporting the age of the conversation.
  const wallLimitHours = approvalPlan?.slurm_request?.max_time;
  const runElapsed =
    isActive && conversation?.started_at
      ? formatElapsed((now - new Date(conversation.started_at).getTime()) / 1000)
        + (wallLimitHours ? ` / ${formatDurationHours(wallLimitHours)}` : '')
      : null;
  const limitsSource = approvalPlan?.limits_source ?? null;

  // Editable Slurm fields: the plan's request seeds the values (TWAIN's
  // suggestion — ~1 CPU per atom of the system); the researcher's edits (if
  // made on this approval card) override them, clamped to the node ceilings.
  const seededSlurmDraft: SlurmDraft | null = approvalPlan?.slurm_request
    ? {
        cpu_count: String(approvalPlan.slurm_request.cpu_count ?? 8),
        gpu_count: String(approvalPlan.slurm_request.gpu_count ?? 0),
        ram: String(Math.max(MIN_RAM_GB, approvalPlan.slurm_request.ram ?? 16)),
        // Seeded readably: a 10-minute cap shows as "10m", not "0.17". The field
        // accepts either form back (parseDurationHours).
        max_time: formatDurationHours(approvalPlan.slurm_request.max_time ?? 0.17),
      }
    : null;
  const slurmDraft =
    slurmEdit && slurmEdit.key === approvalContent ? slurmEdit.draft : seededSlurmDraft;
  const setSlurmDraft = (draft: SlurmDraft) =>
    setSlurmEdit({ key: approvalContent ?? '', draft });

  const refresh = useCallback(async (id: string) => {
    try {
      setConversation(await apiClient.getConversation(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load conversation');
    }
  }, []);

  // Load an existing conversation when navigated to with an id (Browse/resume).
  useEffect(() => {
    if (!params.id || conversation) return;
    if (authLoading || !isAuthenticated) return;   // no token yet -> would 401
    let cancelled = false;
    (async () => {
      try {
        const loaded = await apiClient.getConversation(params.id as string);
        if (!cancelled) setConversation(loaded);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load conversation');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [params.id, conversation, authLoading, isAuthenticated]);

  // Poll while the run is active; stop once it reaches a terminal state.
  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (conversationId && isActive) {
      pollRef.current = setInterval(() => refresh(conversationId), POLL_MS);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [conversationId, isActive, refresh]);

  useEffect(() => {
    scrollRef.current?.scrollToEnd({ animated: true });
  }, [conversation?.messages?.length]);

  // Show what has already been reported for this run, so a second report is a
  // deliberate choice rather than an accidental duplicate.
  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    (async () => {
      try {
        const issues = await apiClient.listRunIssues(conversationId);
        if (!cancelled) setReportedIssues(issues);
      } catch {
        // Non-essential: the report button still works without this.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (!conversation) {
        const parsed = budget.trim() ? Number(budget) : NaN;
        const maxCost = Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
        const created = await apiClient.startConversation(text, maxCost);
        setConversation(created);
      } else {
        await apiClient.sendMessage(conversation.id, text);
        await refresh(conversation.id);
      }
      setInput('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong');
    } finally {
      setBusy(false);
    }
  };

  // Answer a one-tap gate (heavy-calc yes/no, accept-or-rerun). The word sent is
  // exactly what the state machine's parser expects from a typed reply, so the
  // buttons and the composer are interchangeable.
  const handleQuickReply = async (answer: 'yes' | 'no' | 'accept' | 'rerun') => {
    if (!conversation || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiClient.sendMessage(conversation.id, answer);
      await refresh(conversation.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to send your answer');
    } finally {
      setBusy(false);
    }
  };

  const handleTerminate = async () => {
    if (!conversation || cancelling) return;
    setError(null);
    try {
      await apiClient.terminateConversation(conversation.id);
      await refresh(conversation.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to terminate the run');
    }
  };

  const handleApproval = async (decision: 'approve' | 'reject') => {
    if (!conversation || busy) return;
    setBusy(true);
    setError(null);
    try {
      let overrides:
        | { cpu_count: number; gpu_count: number; ram: number; max_time: number }
        | undefined;
      if (decision === 'approve' && slurmDraft) {
        // Clamp each field to the cluster's per-node ceiling (when known) so
        // an over-ask can't produce an unschedulable sbatch.
        const cap = (v: number, max?: number) => (max != null ? Math.min(v, max) : v);
        overrides = {
          cpu_count: cap(Math.max(1, parseInt(slurmDraft.cpu_count, 10) || 8), slurmLimits?.cpu_count),
          gpu_count: cap(Math.max(0, parseInt(slurmDraft.gpu_count, 10) || 0), slurmLimits?.gpu_count),
          ram: cap(Math.max(MIN_RAM_GB, parseInt(slurmDraft.ram, 10) || MIN_RAM_GB), slurmLimits?.ram),
          // Accepts "90m" / "1.5h" / a bare number of hours (see
          // parseDurationHours: a bare number stays HOURS, so an existing plan
          // cannot silently shrink 60x). An unparseable entry falls back to the
          // plan's own suggestion rather than to zero.
          max_time: cap(
            Math.max(
              10 / 60,
              parseDurationHours(slurmDraft.max_time)
                ?? approvalPlan?.slurm_request?.max_time
                ?? 0.17,
            ),
            slurmLimits?.max_time,
          ),
        };
      }
      await apiClient.sendApproval(conversation.id, decision, overrides);
      await refresh(conversation.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to submit decision');
    } finally {
      setBusy(false);
    }
  };

  // Mid-session revision: on a finished run, a typed message becomes "here's
  // what to change" — TWAIN folds it into the run's intent, re-plans, and
  // posts a fresh plan for approval, all within this conversation.
  const handleRevise = async () => {
    const text = input.trim();
    if (!text || !conversationId || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiClient.rerunConversation(conversationId, 'DISCOVER', text);
      setInput('');
      await refresh(conversationId);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to revise the run');
    } finally {
      setBusy(false);
    }
  };

  /** Open the editor for a stage, seeded with whatever that stage can change. */
  const openRerunDraft = (stage: { state: string; label: string }) =>
    setRerunDraft({
      state: stage.state,
      label: stage.label,
      note: '',
      request: stage.state === 'INTAKE' ? openingRequest : null,
      slurm:
        AFTER_PLAN_STAGES.includes(stage.state) && approvalPlan?.slurm_request
          ? {
              cpu_count: String(approvalPlan.slurm_request.cpu_count ?? 8),
              gpu_count: String(approvalPlan.slurm_request.gpu_count ?? 0),
              ram: String(Math.max(MIN_RAM_GB, approvalPlan.slurm_request.ram ?? 16)),
              max_time: formatDurationHours(approvalPlan.slurm_request.max_time ?? 0.17),
            }
          : null,
    });

  const submitRerunDraft = async () => {
    if (!rerunDraft) return;
    const { state, note, request, slurm } = rerunDraft;
    await handleRerun(
      state,
      request?.trim() || undefined,
      note.trim() || undefined,
      slurm
        ? {
            cpu_count: Math.max(1, parseInt(slurm.cpu_count, 10) || 8),
            gpu_count: Math.max(0, parseInt(slurm.gpu_count, 10) || 0),
            ram: Math.max(MIN_RAM_GB, parseInt(slurm.ram, 10) || MIN_RAM_GB),
            max_time: Math.max(
              10 / 60,
              parseDurationHours(slurm.max_time)
                ?? approvalPlan?.slurm_request?.max_time
                ?? 0.17,
            ),
          }
        : undefined,
    );
  };

  const handleRerun = async (
    state: string,
    request?: string,
    feedback?: string,
    slurmRequest?: { cpu_count: number; gpu_count: number; ram: number; max_time: number },
  ) => {
    if (!conversationId || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiClient.rerunConversation(
        conversationId, state, feedback, request, slurmRequest);
      setRerunOpen(false);
      setRerunDraft(null);
      // Reload the full conversation (now `running` at `state`, with the marker
      // message); the poll effect restarts automatically once it's active again.
      await refresh(conversationId);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to re-run from that step');
    } finally {
      setBusy(false);
    }
  };

  // The stages this run reached (so the picker only offers steps that ran).
  // Loop states are anchored back onto the spine so ending mid-loop still
  // enables the stages the run actually passed through.
  const reachedState = conversation
    ? LOOP_STATE_ANCHOR[conversation.current_state] ?? conversation.current_state
    : null;
  const reachedIndex = reachedState ? PIPELINE_STATES.indexOf(reachedState) : -1;

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
          {conversation?.title ?? 'New simulation'}
        </Text>
        {/* Terminate only while the run can still be stopped; Report for as long
            as a run exists to attach. Neither is meaningful before that, so the
            spacer keeps the title centred on a brand-new screen. */}
        {conversation && isActive ? (
          <TouchableOpacity
            style={[styles.terminateBtn, cancelling && styles.disabled]}
            onPress={handleTerminate}
            disabled={cancelling}
            accessibilityRole="button"
          >
            <Text style={styles.terminateText}>
              {cancelling ? 'Terminating…' : 'Terminate'}
            </Text>
          </TouchableOpacity>
        ) : null}
        {conversationId ? (
          <TouchableOpacity
            onPress={() => setReporting(true)}
            accessibilityRole="button"
            accessibilityLabel="Report an issue with this run"
          >
            <Text style={styles.report}>Report</Text>
          </TouchableOpacity>
        ) : (
          <View style={{ width: 48 }} />
        )}
      </View>

      {conversation && (
        <View style={styles.targetBadgeRow}>
          {/* All runs execute on the RIS cluster; the badge just confirms which one. */}
          <Text style={[styles.targetBadge, styles.targetBadgeSlurm]}>
            {`RIS / Slurm${approvalPlan?.slurm_cluster ? ` · ${approvalPlan.slurm_cluster}` : ''}`}
          </Text>
          {/* Live running time. A multi-hour DFT job is otherwise indistinguishable
              from a hung one, which is what sent us looking at the cluster by hand.
              Paired with the wall-time cap when the plan is at hand, so the number
              the researcher set on the approval card is visible against the clock
              it is racing. Ticks only while the run is active. */}
          {runElapsed && (
            <Text style={[styles.targetBadge, styles.elapsedBadge]}>{runElapsed}</Text>
          )}
        </View>
      )}

      {conversation && <StateStepper current={conversation.current_state} status={status} />}

      <ScrollView ref={scrollRef} style={styles.scroll} contentContainerStyle={styles.scrollContent}>
        {!conversation && (
          <Text style={styles.hint}>
            Describe the simulation you want to run — for example, “predict the aqueous
            solubility of aspirin at 25°C.” TWAIN will plan it, ask you to approve, then run it.
          </Text>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {isActive && !awaitingApproval && (
          <View style={styles.working}>
            <ActivityIndicator color={C.washuRed} />
            <Text style={styles.workingText}>
              {cancelling
                ? 'Terminating the run…'
                : status === 'awaiting_input'
                  ? 'Waiting for your answer…'
                  : 'Working…'}
            </Text>
          </View>
        )}
      </ScrollView>

      {reportedIssues.length > 0 && (
        <Text style={styles.reportedNote}>
          {reportedIssues.length === 1 ? '1 issue' : `${reportedIssues.length} issues`} reported for
          this run
          {reportedIssues[0].issue_number ? ` (latest: #${reportedIssues[0].issue_number})` : ''}.
        </Text>
      )}

      {error && <Text style={styles.error}>{error}</Text>}

      {conversationId && (
        <ReportIssueModal
          visible={reporting}
          conversationId={conversationId}
          onClose={() => setReporting(false)}
          onSubmitted={(issue) => setReportedIssues((prev) => [issue, ...prev])}
        />
      )}

      {awaitingApproval ? (
        <View style={styles.approvalBar}>
          <Text style={styles.approvalLabel}>Approve this plan for the RIS cluster?</Text>
          {engineNote ? (
            <View style={styles.engineNotice}>
              <Text style={styles.engineNoticeText}>
                {blockedEngines} fits this request best but isn’t available on the cluster,
                so this plan uses a substitute. Approve to run it as planned, or ask the
                team to make {blockedEngines} available.
              </Text>
              <TouchableOpacity
                style={styles.engineIssueBtn}
                onPress={() => setIssueOpen(true)}
                accessibilityRole="button"
              >
                <Text style={styles.engineIssueText}>
                  Request {blockedEngines} via GitHub issue
                </Text>
              </TouchableOpacity>
            </View>
          ) : null}
          {slurmDraft && (
            <View style={styles.slurmEditor}>
              <Text style={styles.slurmEditorTitle}>
                Resources — suggested by TWAIN, editable
              </Text>
              <Text style={styles.slurmSuggestHint}>
                These are TWAIN’s starting figures, not requirements. Change any of
                them before approving.
              </Text>
              {slurmRationale?.cpu_count ? (
                <Text style={styles.slurmRationale}>{slurmRationale.cpu_count}</Text>
              ) : null}
              {slurmRationale?.max_time ? (
                <Text style={styles.slurmRationale}>{slurmRationale.max_time}</Text>
              ) : null}
              {slurmLimits ? (
                <Text style={styles.slurmLimitsLine}>
                  {`Most you can request${limitsSource ? ` (${limitsSource})` : ''}: ${[
                    slurmLimits.cpu_count != null ? `${slurmLimits.cpu_count} CPUs` : null,
                    slurmLimits.gpu_count != null ? `${slurmLimits.gpu_count} GPUs` : null,
                    slurmLimits.ram != null ? `${slurmLimits.ram} GB RAM` : null,
                    slurmLimits.max_time != null ? `${slurmLimits.max_time} h wall` : null,
                  ]
                    .filter(Boolean)
                    .join(' · ')}`}
                </Text>
              ) : null}
              <View style={styles.slurmRow}>
                <SlurmField
                  label={`CPUs — suggested${slurmLimits?.cpu_count != null ? `, max ${slurmLimits.cpu_count}` : ''}`}
                  value={slurmDraft.cpu_count}
                  onChange={(v) => setSlurmDraft({ ...slurmDraft, cpu_count: v })}
                />
                <SlurmField
                  label={`GPUs — suggested${slurmLimits?.gpu_count != null ? `, max ${slurmLimits.gpu_count}` : ''}`}
                  value={slurmDraft.gpu_count}
                  onChange={(v) => setSlurmDraft({ ...slurmDraft, gpu_count: v })}
                />
              </View>
              <View style={styles.slurmRow}>
                <SlurmField
                  label={
                    slurmLimits?.ram != null
                      ? `RAM GB — suggested, ${MIN_RAM_GB}–${slurmLimits.ram}`
                      : `RAM GB — suggested, min ${MIN_RAM_GB}`
                  }
                  value={slurmDraft.ram}
                  onChange={(v) => setSlurmDraft({ ...slurmDraft, ram: v })}
                />
                <SlurmField
                  label={`Wall time — e.g. 90m or 1.5h${slurmLimits?.max_time != null ? `, max ${formatDurationHours(slurmLimits.max_time)}` : ''}`}
                  value={slurmDraft.max_time}
                  onChange={(v) => setSlurmDraft({ ...slurmDraft, max_time: v })}
                />
              </View>
            </View>
          )}
          <View style={styles.approvalButtons}>
            <TouchableOpacity
              style={[styles.approveBtn, busy && styles.disabled]}
              onPress={() => handleApproval('approve')}
              accessibilityRole="button"
            >
              <Text style={styles.approveText}>Approve & submit to RIS</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.rejectBtn, busy && styles.disabled]}
              onPress={() => handleApproval('reject')}
              accessibilityRole="button"
            >
              <Text style={styles.rejectText}>Reject</Text>
            </TouchableOpacity>
          </View>
        </View>
      ) : isTerminal ? (
        <View style={styles.terminalBar}>
          <Text style={styles.terminalText}>{terminalMessage}</Text>
          <View style={styles.terminalButtons}>
            <TouchableOpacity
              style={[styles.rerunBtn, busy && styles.disabled]}
              onPress={() => setRerunOpen(true)}
              disabled={busy}
              accessibilityRole="button"
            >
              <Text style={styles.rerunText}>↩︎ Re-run from…</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={styles.reportBtn}
              onPress={() =>
                router.push({ pathname: '/report', params: { id: conversationId as string } })
              }
              accessibilityRole="button"
            >
              <Text style={styles.reportText}>
                {status === 'completed' ? 'View results' : 'View report'}
              </Text>
            </TouchableOpacity>
          </View>
          {/* Mid-session revision: type what should change and the run
              re-plans with it, ending in a fresh approval card. */}
          <View style={styles.inputBar}>
            <TextInput
              style={styles.input}
              value={input}
              onChangeText={setInput}
              placeholder="Want changes? Describe what to update and TWAIN will revise the plan…"
              placeholderTextColor={C.textSecondary}
              editable={!busy}
              onSubmitEditing={handleRevise}
              multiline
            />
            <TouchableOpacity
              style={[styles.sendBtn, (busy || !input.trim()) && styles.disabled]}
              onPress={handleRevise}
              disabled={busy || !input.trim()}
              accessibilityRole="button"
            >
              <Text style={styles.sendText}>Revise</Text>
            </TouchableOpacity>
          </View>
        </View>
      ) : yesNoPending ? (
        <View style={styles.approvalBar}>
          <Text style={styles.approvalLabel}>Run the heavy calculation now?</Text>
          <View style={styles.approvalButtons}>
            <TouchableOpacity
              style={[styles.approveBtn, busy && styles.disabled]}
              onPress={() => handleQuickReply('yes')}
              disabled={busy}
              accessibilityRole="button"
            >
              <Text style={styles.approveText}>Yes, run it</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.rejectBtn, busy && styles.disabled]}
              onPress={() => handleQuickReply('no')}
              disabled={busy}
              accessibilityRole="button"
            >
              <Text style={styles.rejectText}>No, don’t run it</Text>
            </TouchableOpacity>
          </View>
        </View>
      ) : acceptOrRerunPending ? (
        <View style={styles.approvalBar}>
          <Text style={styles.approvalLabel}>Accept this result, or re-run to improve it?</Text>
          <Text style={styles.approvalNote}>
            A method can be right and still miss a reference it was never meant to
            reproduce. Re-running costs another full calculation.
          </Text>
          <View style={styles.approvalButtons}>
            <TouchableOpacity
              style={[styles.approveBtn, busy && styles.disabled]}
              onPress={() => handleQuickReply('accept')}
              disabled={busy}
              accessibilityRole="button"
            >
              <Text style={styles.approveText}>Accept this result</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.neutralBtn, busy && styles.disabled]}
              onPress={() => setRerunOpen(true)}
              disabled={busy}
              accessibilityRole="button"
            >
              <Text style={styles.neutralText}>Re-run from…</Text>
            </TouchableOpacity>
          </View>
        </View>
      ) : (
        <View style={styles.composer}>
          {!conversation && (
            <View style={styles.budgetRow}>
              <Text style={styles.budgetLabel}>Budget $</Text>
              <TextInput
                style={styles.budgetInput}
                value={budget}
                onChangeText={setBudget}
                placeholder="default"
                placeholderTextColor={C.textSecondary}
                keyboardType="decimal-pad"
                editable={!busy}
              />
              <Text style={styles.budgetHint}>optional — caps LLM spend for this run</Text>
            </View>
          )}
          <View style={styles.inputBar}>
            <TextInput
              style={styles.input}
              value={input}
              onChangeText={setInput}
              placeholder={conversation ? 'Type your reply…' : 'Describe your simulation…'}
              placeholderTextColor={C.textSecondary}
              editable={!busy && (!conversation || isActive)}
              onSubmitEditing={handleSend}
              multiline
            />
            <TouchableOpacity
              style={[styles.sendBtn, (busy || (!conversation && !input.trim())) && styles.disabled]}
              onPress={handleSend}
              accessibilityRole="button"
            >
              <Text style={styles.sendText}>{conversation ? 'Send' : 'Start'}</Text>
            </TouchableOpacity>
          </View>
        </View>
      )}

      <Modal
        visible={rerunOpen}
        transparent
        animationType="fade"
        onRequestClose={() => {
          if (busy) return;
          setRerunOpen(false);
          setRerunDraft(null);
        }}
      >
        <View style={styles.modalBackdrop}>
          <View style={styles.modalCard}>
            {rerunDraft === null ? (
              <>
                <Text style={styles.modalTitle}>Re-run from a step</Text>
                <Text style={styles.modalHint}>
                  Pick a step to restart from. That step and everything after it run again;
                  the earlier steps are kept.
                </Text>
                <ScrollView style={styles.stageList}>
                  {RERUN_STAGES.map((stage) => {
                    const stageIndex = PIPELINE_STATES.indexOf(stage.state);
                    // Offer only steps the run actually reached.
                    const enabled = reachedIndex >= 0 && stageIndex <= reachedIndex;
                    return (
                      <TouchableOpacity
                        key={stage.state}
                        style={[styles.stageRow, (!enabled || busy) && styles.disabled]}
                        // Every stage opens its editor first: restarting a step
                        // without being able to say what should differ just
                        // reruns the same inputs and gets the same answer.
                        onPress={() => openRerunDraft(stage)}
                        disabled={!enabled || busy}
                        accessibilityRole="button"
                        accessibilityState={{ disabled: !enabled || busy }}
                      >
                        <View style={styles.stageMain}>
                          <Text style={styles.stageLabel}>{stage.label}</Text>
                          <Text style={styles.stageDesc}>{stage.desc}</Text>
                        </View>
                        <Text style={styles.stageChevron}>›</Text>
                      </TouchableOpacity>
                    );
                  })}
                </ScrollView>
                <TouchableOpacity
                  style={styles.modalCancel}
                  onPress={() => setRerunOpen(false)}
                  disabled={busy}
                  accessibilityRole="button"
                >
                  <Text style={styles.modalCancelText}>Cancel</Text>
                </TouchableOpacity>
              </>
            ) : (
              <>
                <Text style={styles.modalTitle}>{`Re-run from ${rerunDraft.label}`}</Text>
                <Text style={styles.modalHint}>
                  {rerunDraft.request !== null
                    ? 'Intake reads this prompt again, so you can change what you asked for. Everything after it is re-derived.'
                    : `${rerunDraft.label} and every step after it run again. Say what should be different and TWAIN takes it into account.`}
                </Text>
                <ScrollView style={styles.stageList}>
                  {/* INTAKE: the prompt itself is the editable input. */}
                  {rerunDraft.request !== null && (
                    <TextInput
                      style={styles.promptInput}
                      value={rerunDraft.request}
                      onChangeText={(v) => setRerunDraft({ ...rerunDraft, request: v })}
                      placeholder="Describe the simulation you want"
                      placeholderTextColor={C.textSecondary}
                      multiline
                      editable={!busy}
                      accessibilityLabel="Edited request"
                    />
                  )}
                  {/* Every stage: what should be different this time. */}
                  <TextInput
                    style={styles.promptInput}
                    value={rerunDraft.note}
                    onChangeText={(v) => setRerunDraft({ ...rerunDraft, note: v })}
                    placeholder={
                      rerunDraft.request !== null
                        ? 'Anything else to change (optional)'
                        : 'What should be different this time? (optional)'
                    }
                    placeholderTextColor={C.textSecondary}
                    multiline
                    editable={!busy}
                    accessibilityLabel="What to change on this re-run"
                  />
                  {/* After PLAN: the existing plan survives, so its resources are
                      editable here. Before PLAN a fresh plan would overwrite them,
                      which is why no fields are offered (and the API refuses). */}
                  {rerunDraft.slurm && (
                    <View style={styles.slurmGrid}>
                      <Text style={styles.slurmNote}>
                        Re-uses the existing plan with these resources.
                      </Text>
                      <SlurmField
                        label="CPUs"
                        value={rerunDraft.slurm.cpu_count}
                        onChange={(v) =>
                          setRerunDraft({
                            ...rerunDraft,
                            slurm: { ...rerunDraft.slurm!, cpu_count: v },
                          })
                        }
                      />
                      <SlurmField
                        label="RAM GB"
                        value={rerunDraft.slurm.ram}
                        onChange={(v) =>
                          setRerunDraft({
                            ...rerunDraft,
                            slurm: { ...rerunDraft.slurm!, ram: v },
                          })
                        }
                      />
                      <SlurmField
                        label="Wall time — e.g. 90m or 1.5h"
                        value={rerunDraft.slurm.max_time}
                        onChange={(v) =>
                          setRerunDraft({
                            ...rerunDraft,
                            slurm: { ...rerunDraft.slurm!, max_time: v },
                          })
                        }
                      />
                    </View>
                  )}
                </ScrollView>
                <View style={styles.approvalButtons}>
                  <TouchableOpacity
                    style={[
                      styles.approveBtn,
                      (busy || (rerunDraft.request !== null && !rerunDraft.request.trim()))
                        && styles.disabled,
                    ]}
                    onPress={submitRerunDraft}
                    disabled={
                      busy || (rerunDraft.request !== null && !rerunDraft.request.trim())
                    }
                    accessibilityRole="button"
                  >
                    <Text style={styles.approveText}>Re-run with this</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[styles.neutralBtn, busy && styles.disabled]}
                    onPress={() => setRerunDraft(null)}
                    disabled={busy}
                    accessibilityRole="button"
                  >
                    <Text style={styles.neutralText}>Back</Text>
                  </TouchableOpacity>
                </View>
              </>
            )}
          </View>
        </View>
      </Modal>

      <IssueModal
        visible={issueOpen}
        submitterEmail={user?.email}
        initialTitle={`Provision request: ${blockedEngines ?? 'engine'} unavailable on the RIS cluster`}
        initialBody={engineIssueBody}
        onSubmit={(title, body) => apiClient.createIssue(title, body)}
        onClose={() => setIssueOpen(false)}
      />
    </SafeAreaView>
  );
};

const SlurmField: React.FC<{
  label: string;
  value: string;
  onChange: (v: string) => void;
}> = ({ label, value, onChange }) => (
  <View style={styles.slurmField}>
    <Text style={styles.slurmFieldLabel}>{label}</Text>
    <TextInput
      style={styles.slurmFieldInput}
      value={value}
      onChangeText={onChange}
      keyboardType="decimal-pad"
      accessibilityLabel={label}
    />
  </View>
);

const StateStepper: React.FC<{ current: string; status?: string }> = ({ current, status }) => {
  const currentIndex = PIPELINE_STATES.indexOf(current);
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      style={styles.stepper}
      contentContainerStyle={styles.stepperContent}
    >
      {PIPELINE_STATES.map((state, i) => {
        const done = currentIndex > i || status === 'completed';
        const active = currentIndex === i && status !== 'completed';
        return (
          <View key={state} style={styles.step}>
            <View
              style={[
                styles.dot,
                done && styles.dotDone,
                active && styles.dotActive,
              ]}
            />
            <Text style={[styles.stepLabel, active && styles.stepLabelActive]}>{state}</Text>
          </View>
        );
      })}
    </ScrollView>
  );
};

const MessageBubble: React.FC<{ message: Message }> = ({ message }) => {
  const isUser = message.role === 'user';
  if (message.kind === 'terminate') {
    return <Text style={styles.terminateNote}>You asked to terminate this run.</Text>;
  }
  if (message.kind === 'approval_request') {
    return <PlanCard content={message.content} />;
  }
  return (
    <View style={[styles.bubble, isUser ? styles.userBubble : styles.assistantBubble]}>
      {message.kind === 'clarification' && (
        <Text style={styles.bubbleTag}>Clarification</Text>
      )}
      <Text style={[styles.bubbleText, isUser && styles.userBubbleText]}>{message.content}</Text>
    </View>
  );
};

// Renders the approval-gate plan: leads with the plain-language summary of what
// the run will do, then the concrete method / system / cost / notes.
const PlanCard: React.FC<{ content: string }> = ({ content }) => {
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
      {plan.requested_property ? <PlanRow label="Property" value={plan.requested_property} /> : null}
      {methodText ? (
        <PlanRow label="Method" value={libs ? `${methodText}  ·  ${libs}` : methodText} />
      ) : null}
      {costText ? <PlanRow label="Estimated cost" value={costText} /> : null}
      {slurm ? (
        <PlanRow
          label="Slurm ask"
          value={`${slurm.cpu_count ?? '—'} CPU, ${slurm.gpu_count ?? 0} GPU, ${
            slurm.ram ?? '—'
          } GB RAM, ${slurm.max_time ?? '—'} h`}
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
  title: { color: C.washuWhite, fontSize: 16, fontWeight: '700', flex: 1, textAlign: 'center' },
  report: { color: C.washuWhite, fontSize: 14, fontWeight: '600', width: 56, textAlign: 'right' },
  reportedNote: {
    fontSize: 12,
    color: C.textSecondary,
    paddingHorizontal: Spacing.three,
    paddingBottom: Spacing.one,
  },
  terminateBtn: {
    borderWidth: 1,
    borderColor: C.washuWhite,
    borderRadius: 6,
    paddingHorizontal: Spacing.two,
    paddingVertical: 4,
  },
  terminateText: { color: C.washuWhite, fontSize: 12, fontWeight: '700' },
  terminateNote: {
    alignSelf: 'center',
    color: C.textSecondary,
    fontSize: 12,
    fontStyle: 'italic',
    marginVertical: Spacing.one,
  },
  targetBadgeRow: {
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.one,
    backgroundColor: C.backgroundElement,
  },
  targetBadge: {
    alignSelf: 'flex-start',
    fontSize: 12,
    fontWeight: '700',
    paddingHorizontal: Spacing.two,
    paddingVertical: 4,
    overflow: 'hidden',
  },
  targetBadgeSlurm: { color: C.washuGreen },
  // Tabular figures so a ticking counter doesn't shuffle its own width each second.
  elapsedBadge: { color: C.textSecondary, fontVariant: ['tabular-nums'] },
  stepper: { maxHeight: 62, backgroundColor: C.backgroundElement, flexGrow: 0 },
  stepperContent: { alignItems: 'center', paddingHorizontal: Spacing.three, gap: Spacing.three },
  step: { alignItems: 'center', gap: 4, paddingVertical: Spacing.two },
  dot: { width: 12, height: 12, borderRadius: 6, backgroundColor: C.borderStrong },
  dotDone: { backgroundColor: C.washuGreen },
  dotActive: { backgroundColor: C.washuRed, transform: [{ scale: 1.3 }] },
  stepLabel: { fontSize: 9, color: C.textSecondary },
  stepLabelActive: { color: C.washuRed, fontWeight: '700' },
  scroll: { flex: 1 },
  scrollContent: { padding: Spacing.three, gap: Spacing.two },
  hint: { color: C.textSecondary, fontSize: 15, lineHeight: 22, padding: Spacing.two },
  bubble: { maxWidth: '85%', borderRadius: 12, padding: Spacing.three },
  userBubble: { alignSelf: 'flex-end', backgroundColor: C.washuRed },
  assistantBubble: { alignSelf: 'flex-start', backgroundColor: C.backgroundElement },
  bubbleTag: { fontSize: 10, fontWeight: '700', color: C.washuGreen, marginBottom: 4 },
  bubbleText: { fontSize: 15, color: C.text, lineHeight: 21 },
  userBubbleText: { color: C.washuWhite },
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
  working: { flexDirection: 'row', alignItems: 'center', gap: Spacing.two, padding: Spacing.two },
  workingText: { color: C.textSecondary, fontSize: 13 },
  error: { color: C.washuRed, paddingHorizontal: Spacing.three, paddingVertical: Spacing.one },
  composer: {
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
  },
  budgetRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    paddingHorizontal: Spacing.two,
    paddingTop: Spacing.two,
  },
  budgetLabel: { fontSize: 14, fontWeight: '600', color: C.text },
  budgetInput: {
    width: 80,
    height: 36,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: C.border,
    paddingHorizontal: Spacing.two,
    fontSize: 15,
    color: C.text,
    backgroundColor: C.washuWhite,
  },
  budgetHint: { flex: 1, fontSize: 12, color: C.textSecondary },
  inputBar: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: Spacing.two,
    padding: Spacing.two,
  },
  input: {
    flex: 1,
    maxHeight: 120,
    minHeight: 44,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.border,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    fontSize: 15,
    color: C.text,
    backgroundColor: C.washuWhite,
  },
  sendBtn: {
    backgroundColor: C.washuRed,
    borderRadius: 10,
    paddingHorizontal: Spacing.four,
    height: 44,
    justifyContent: 'center',
  },
  sendText: { color: C.washuWhite, fontWeight: '700', fontSize: 15 },
  approvalBar: {
    padding: Spacing.three,
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
    gap: Spacing.two,
  },
  approvalLabel: { fontSize: 15, fontWeight: '600', color: C.text },
  engineNotice: {
    gap: Spacing.two,
    padding: Spacing.two,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.washuRed,
    backgroundColor: C.errorSurface,
  },
  engineNoticeText: { fontSize: 13, color: C.text, lineHeight: 19 },
  engineIssueBtn: {
    alignSelf: 'flex-start',
    borderWidth: 1,
    borderColor: C.washuRed,
    borderRadius: 6,
    paddingHorizontal: Spacing.two,
    paddingVertical: 6,
  },
  engineIssueText: { color: C.washuRed, fontSize: 13, fontWeight: '700' },
  slurmEditor: {
    gap: Spacing.two,
    padding: Spacing.two,
    backgroundColor: C.backgroundElement,
    borderRadius: 10,
  },
  slurmEditorTitle: { fontSize: 13, fontWeight: '700', color: C.text },
  slurmRationale: { fontSize: 12, color: C.text, lineHeight: 17 },
  slurmLimitsLine: { fontSize: 12, fontWeight: '600', color: C.textSecondary },
  slurmSuggestHint: { fontSize: 12, color: C.textSecondary, lineHeight: 17 },
  slurmRow: { flexDirection: 'row', gap: Spacing.two },
  // Resource fields inside the re-run editor (stacked, unlike the approval card's
  // side-by-side row, because the modal is narrower).
  slurmGrid: { gap: Spacing.one, marginTop: Spacing.two },
  slurmNote: { fontSize: 12, color: C.textSecondary },
  slurmField: { flex: 1, gap: 4 },
  slurmFieldLabel: { fontSize: 11, color: C.textSecondary, fontWeight: '600' },
  slurmFieldInput: {
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 8,
    paddingHorizontal: Spacing.two,
    paddingVertical: Spacing.one,
    backgroundColor: C.washuWhite,
    fontSize: 14,
    color: C.text,
  },
  approvalButtons: { flexDirection: 'row', gap: Spacing.two },
  approveBtn: {
    flex: 1,
    backgroundColor: C.washuGreen,
    borderRadius: 10,
    paddingVertical: Spacing.three,
    alignItems: 'center',
  },
  approveText: { color: C.washuWhite, fontWeight: '700', fontSize: 15 },
  rejectBtn: {
    flex: 1,
    backgroundColor: C.washuWhite,
    borderWidth: 1,
    borderColor: C.washuRed,
    borderRadius: 10,
    paddingVertical: Spacing.three,
    alignItems: 'center',
  },
  rejectText: { color: C.washuRed, fontWeight: '700', fontSize: 15 },
  // Re-running is an alternative, not a rejection, so it gets a neutral outline
  // rather than the red one the reject button uses.
  neutralBtn: {
    flex: 1,
    backgroundColor: C.washuWhite,
    borderWidth: 1,
    borderColor: C.textSecondary,
    borderRadius: 10,
    paddingVertical: Spacing.three,
    alignItems: 'center',
  },
  neutralText: { color: C.text, fontWeight: '700', fontSize: 15 },
  promptInput: {
    minHeight: 96,
    maxHeight: 200,
    borderWidth: 1,
    borderColor: C.textSecondary,
    borderRadius: 10,
    padding: Spacing.three,
    fontSize: 15,
    color: C.text,
    backgroundColor: C.washuWhite,
    textAlignVertical: 'top',
  },
  approvalNote: { fontSize: 13, color: C.textSecondary, lineHeight: 18 },
  terminalBar: {
    gap: Spacing.two,
    padding: Spacing.three,
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
  },
  terminalText: { fontSize: 14, color: C.textSecondary },
  terminalButtons: { flexDirection: 'row', gap: Spacing.two },
  rerunBtn: {
    flex: 1,
    backgroundColor: C.washuWhite,
    borderWidth: 1,
    borderColor: C.washuRed,
    borderRadius: 10,
    paddingVertical: Spacing.three,
    alignItems: 'center',
    justifyContent: 'center',
  },
  rerunText: { color: C.washuRed, fontWeight: '700', fontSize: 15 },
  reportBtn: {
    flex: 1,
    backgroundColor: C.washuGreen,
    borderRadius: 10,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.three,
    alignItems: 'center',
    justifyContent: 'center',
  },
  reportText: { color: C.washuWhite, fontWeight: '700', fontSize: 15 },
  modalBackdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.45)',
    alignItems: 'center',
    justifyContent: 'center',
    padding: Spacing.four,
  },
  modalCard: {
    width: '100%',
    maxWidth: 380,
    maxHeight: '80%',
    backgroundColor: C.washuWhite,
    borderRadius: 12,
    padding: Spacing.four,
    gap: Spacing.two,
  },
  modalTitle: { fontSize: 17, fontWeight: '700', color: C.text },
  modalHint: { fontSize: 13, color: C.textSecondary, lineHeight: 18 },
  stageList: { flexGrow: 0, marginVertical: Spacing.one },
  stageRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.three,
    borderRadius: 10,
    backgroundColor: C.backgroundElement,
    marginBottom: Spacing.two,
  },
  stageMain: { flex: 1, gap: 2 },
  stageLabel: { fontSize: 15, fontWeight: '700', color: C.text },
  stageDesc: { fontSize: 12, color: C.textSecondary },
  stageChevron: { fontSize: 22, color: C.washuRed, fontWeight: '400' },
  modalCancel: {
    alignSelf: 'flex-end',
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: C.border,
  },
  modalCancelText: { fontSize: 15, fontWeight: '600', color: C.text },
  disabled: { opacity: 0.5 },
});

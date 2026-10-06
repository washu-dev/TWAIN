import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  Modal,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { apiClient, Conversation, Message, RunIssue } from '@/api/client';
import { IssueModal } from '@/components/IssueModal';
import {
  ApprovedMetric, ApprovedResources, PlanCard, parsePlanSummary,
} from '@/components/PlanCard';
import { PrimaryButton } from '@/components/PrimaryButton';
import { ReportIssueModal } from '@/components/ReportIssueModal';
import { PIPELINE_STATES, StateStepper } from '@/components/StateStepper';
import { MIN_WALL_HOURS, WallTimeField, wallTimeLabel } from '@/components/WallTimeField';
import { useAuth } from '@/hooks/useAuth';
import { useNow } from '@/hooks/useNow';
import { useRunActivity } from '@/hooks/useRunActivity';
import { RunActivity } from '@/components/RunActivity';
import { FailureCard } from '@/components/FailureCard';
import {
  DurationUnit, durationToHours, formatDurationHours, formatElapsed, splitDurationHours,
} from '@/utils/duration';
import { LinearGradient } from 'expo-linear-gradient';
import { PressableScale } from '@/components/Motion';
import { Colors, Gradients, Motion, Radius, Spacing } from '@/constants/theme';

const C = Colors.light;

// Exact lead-in the planner puts on the safety note when the best-fit engine
// can't run on the cluster (see ENGINE_UNAVAILABLE_PREFIX in
// modules/16_agent_mesh_control_plane/statemachine.py — keep in sync). The
// approval card keys off it to offer a one-tap GitHub provisioning request.
const ENGINE_UNAVAILABLE_PREFIX = 'ENGINE UNAVAILABLE ON THIS DEPLOYMENT: ';

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

/** Hours -> the two draft fields that hold them, so seeding reads in one line. */
const wallTimeFields = (hours: number): Pick<SlurmDraft, 'max_time' | 'max_time_unit'> => {
  const { amount, unit } = splitDurationHours(hours);
  return { max_time: amount, max_time_unit: unit };
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
type SlurmNumbers = { cpu_count: number; gpu_count: number; ram: number; max_time: number };
type SlurmCeilings = Partial<SlurmNumbers> | null;

/**
 * Bound a resource ask by the ceilings of the machine that will run it.
 *
 * One function because there were two editors and only one of them clamped. The
 * approval card capped every field and labelled each with its maximum; the
 * re-run form sent whatever was typed. Nothing on the server bounds it either --
 * `apply_slurm_overrides` applies floors only -- so 5000 entered on a re-run
 * reached sbatch verbatim as `--cpus-per-task=5000`, and the job pends forever
 * on a 64-core node with nothing in the UI explaining why.
 */
const clampSlurm = (want: SlurmNumbers, limits: SlurmCeilings): SlurmNumbers => {
  const cap = (v: number, max?: number) => (max != null ? Math.min(v, max) : v);
  return {
    cpu_count: cap(want.cpu_count, limits?.cpu_count),
    gpu_count: cap(want.gpu_count, limits?.gpu_count),
    ram: cap(want.ram, limits?.ram),
    max_time: cap(want.max_time, limits?.max_time),
  };
};

/**
 * One editable acceptance criterion.
 *
 * Kept as strings, like SlurmDraft: an empty field has to mean "no bar" rather
 * than 0, and a half-typed "-" or "1e" must not be coerced mid-keystroke.
 */
type MetricDraft = {
  metric_name: string;
  target_value: string;
  tolerance: string;
};

type SlurmDraft = {
  cpu_count: string;
  gpu_count: string;
  ram: string;
  /** Wall time as a bare NUMBER; `max_time_unit` says what it counts. Split in
      two because the field is numeric-only on purpose: the keypad a phone shows
      for a number has no letters on it, so "90m" was unenterable there and the
      label asking for it was a dead end. */
  max_time: string;
  max_time_unit: DurationUnit;
};

/**
 * The plan's own resource ask, in the shape the editor holds it.
 *
 * One definition because there were two, character-identical, seeding the
 * approval card and the re-run card -- and "two copies of the same seeding logic"
 * is precisely the shape of drift that has to stay impossible here: every
 * "did the researcher edit this?" test in this file works by comparing a draft
 * against this seed, so a seed that differs between the two cards would silently
 * report edits nobody made on one of them.
 */
const seedSlurmDraft = (req: {
  cpu_count?: number | null;
  gpu_count?: number | null;
  ram?: number | null;
  max_time?: number | null;
}): SlurmDraft => ({
  cpu_count: String(req.cpu_count ?? 8),
  gpu_count: String(req.gpu_count ?? 0),
  ram: String(Math.max(MIN_RAM_GB, req.ram ?? 16)),
  // Seeded readably: a 10-minute cap opens as 10 with "min" selected, not as
  // 0.1666 in an hours box.
  ...wallTimeFields(req.max_time ?? 0.17),
});

/**
 * Did the researcher retype anything?
 *
 * Field by field rather than JSON.stringify: the draft is rebuilt by spreading,
 * and a stringify comparison silently depends on key ORDER surviving that. It
 * does today. Making correctness rest on it is how a test that is supposed to
 * mean "nobody touched this" starts quietly answering a different question.
 */
const sameSlurmDraft = (a: SlurmDraft, b: SlurmDraft): boolean =>
  a.cpu_count === b.cpu_count
  && a.gpu_count === b.gpu_count
  && a.ram === b.ram
  && a.max_time === b.max_time
  && a.max_time_unit === b.max_time_unit;

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
  // The researcher's edits to the acceptance criteria, keyed to the same card so a
  // fresh plan reseeds from its own metrics.
  const [metricEdit, setMetricEdit] =
    useState<{ key: string; drafts: MetricDraft[] } | null>(null);
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
  // What the run is doing inside its current stage (checklist + job log).
  const activity = useRunActivity(conversation?.id, isActive);
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

  /**
   * The resource figures the run is actually bounded by.
   *
   * The plan card shows TWAIN's PROPOSAL. If the researcher edited it at the
   * gate, the runner patched the plan and submitted the edited numbers -- so the
   * proposal is no longer what is in force, and anything that reports or reuses
   * "the run's resources" has to read the approved override instead.
   *
   * Reading the proposal instead was wrong in two places at once: the live clock
   * counted a 30-minute run against a 4h cap it did not have (so a job Slurm was
   * about to kill looked like it had hours of headroom), and the re-run editor
   * opened on the proposal, so re-running silently handed back the numbers the
   * researcher had already overridden.
   */
  const approvedSlurmInForce: ApprovedResources | null = (() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].kind !== 'approval_response') continue;
      try {
        const slurm = JSON.parse(messages[i].content)?.slurm_request;
        if (slurm && typeof slurm === 'object') return slurm as ApprovedResources;
      } catch {
        // A bare "approve"/"reject" overrode nothing; keep looking further back.
      }
    }
    return null;
  })();
  const slurmInForce = approvalPlan?.slurm_request
    ? { ...approvalPlan.slurm_request, ...(approvedSlurmInForce ?? {}) }
    : null;

  // "12m 04s / 4h" -- how long this run has been going, against the wall-time cap
  // it was approved with. started_at is the newest run.started, so a resumed run
  // times its current slice rather than reporting the age of the conversation.
  const wallLimitHours = slurmInForce?.max_time;
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
    ? seedSlurmDraft(approvalPlan.slurm_request)
    : null;
  const slurmDraft =
    slurmEdit && slurmEdit.key === approvalContent ? slurmEdit.draft : seededSlurmDraft;
  const setSlurmDraft = (draft: SlurmDraft) =>
    setSlurmEdit({ key: approvalContent ?? '', draft });

  // The acceptance bar, seeded from the plan. A null target seeds as an empty
  // field rather than "0": TWAIN writing null means it had no defensible target,
  // and showing 0 would present a bar it never proposed.
  const seededMetricDrafts: MetricDraft[] = (approvalPlan?.acceptance_metrics ?? [])
    .filter((m) => m?.metric_name)
    .map((m) => ({
      metric_name: String(m.metric_name),
      target_value: m.target_value == null ? '' : String(m.target_value),
      tolerance: m.tolerance == null ? '' : String(m.tolerance),
    }));
  const metricDrafts =
    metricEdit && metricEdit.key === approvalContent ? metricEdit.drafts : seededMetricDrafts;
  const setMetricDraft = (index: number, patch: Partial<MetricDraft>) =>
    setMetricEdit({
      key: approvalContent ?? '',
      drafts: metricDrafts.map((d, i) => (i === index ? { ...d, ...patch } : d)),
    });
  // Sent only when something actually differs from the plan, so an untouched card
  // records no override and the transcript does not claim an edit that never
  // happened.
  //
  // Compared as DRAFTS -- the strings in the boxes -- against the strings the plan
  // seeded them with. That is an exact test of "did the researcher type something
  // different", and it is exact precisely because it never converts: comparing the
  // submitted numbers against the plan's numbers instead reports the app's own
  // normalisation as the researcher's edit. A 0.17h cap seeds the editor as "10"
  // minutes and submits as 10/60 = 0.16666..., which is not 0.17, so every
  // untouched approval of a plan whose wall time was not a whole number of minutes
  // recorded an amendment nobody made, and the plan card then labelled the whole
  // Slurm ask "(yours)". The same applies to the RAM floor and the ceiling clamps.
  const slurmEdited =
    !!slurmDraft && !!seededSlurmDraft && !sameSlurmDraft(slurmDraft, seededSlurmDraft);
  // Whether the PLAN proposed any bar at all. Drives the heading, because "these
  // are TWAIN's figures, editable" and "TWAIN had none, supply one" are different
  // messages and the reader cannot tell them apart from two empty boxes.
  const planProposedATarget = seededMetricDrafts.some(
    (d) => d.target_value !== '' || d.tolerance !== '');
  const metricsEdited =
    metricDrafts.length > 0
    && JSON.stringify(metricDrafts) !== JSON.stringify(seededMetricDrafts);

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
      let overrides: SlurmNumbers | undefined;
      if (decision === 'approve' && slurmDraft) {
        // Clamped to the cluster's per-node ceilings (when known) so an over-ask
        // can't produce an unschedulable sbatch. Same clamp as the re-run form.
        const wanted = clampSlurm(
          {
            cpu_count: Math.max(1, parseInt(slurmDraft.cpu_count, 10) || 8),
            gpu_count: Math.max(0, parseInt(slurmDraft.gpu_count, 10) || 0),
            ram: Math.max(MIN_RAM_GB, parseInt(slurmDraft.ram, 10) || MIN_RAM_GB),
            // The number is typed, the unit is chosen -- so there is no guessing
            // about what a bare "10" meant. An empty or unusable entry falls back
            // to the plan's own suggestion rather than to zero.
            max_time: Math.max(
              MIN_WALL_HOURS,
              durationToHours(slurmDraft.max_time, slurmDraft.max_time_unit)
                ?? approvalPlan?.slurm_request?.max_time
                ?? 0.17,
            ),
          },
          slurmLimits,
        );
        // Send the patch when the researcher retyped something, OR when the
        // figures we would actually run with differ MEANINGFULLY from the plan's
        // own -- which happens when a ceiling or the RAM floor bit, and which
        // still has to reach the runner or the sbatch would be unschedulable.
        //
        // What it must NOT count is the editor's own arithmetic. Planning stores
        // wall time as round(minutes / 60, 4), so a 10-minute cap is 0.1667 while
        // the editor round-trips it to 10/60 = 0.16666..., and comparing those
        // exactly reported an amendment on essentially every sub-hour plan. Half
        // a minute is the line: below it, no researcher could have typed the
        // difference. Same threshold PlanCard uses to decide whether to say
        // "(yours)", so the record and the label can never disagree.
        const plan = approvalPlan?.slurm_request;
        const differsFromPlan = !!plan && (
          wanted.cpu_count !== plan.cpu_count
          || wanted.gpu_count !== (plan.gpu_count ?? 0)
          || wanted.ram !== plan.ram
          || Math.abs(wanted.max_time - (plan.max_time ?? 0)) > 1 / 120
        );
        if (slurmEdited || differsFromPlan) overrides = wanted;
      }
      // Numbers, or null for an empty field: null is "no bar", and coercing a
      // blank to 0 would silently demand the answer be exactly zero.
      const asNumber = (text: string) => {
        const trimmed = text.trim();
        if (!trimmed) return null;
        const value = Number(trimmed);
        return Number.isFinite(value) ? value : null;
      };
      const metrics =
        decision === 'approve' && metricsEdited
          ? metricDrafts.map((d) => ({
              metric_name: d.metric_name,
              target_value: asNumber(d.target_value),
              tolerance: asNumber(d.tolerance),
            }))
          : undefined;
      await apiClient.sendApproval(conversation.id, decision, overrides, metrics);
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
        AFTER_PLAN_STAGES.includes(stage.state) && slurmInForce
          ? seedSlurmDraft(slurmInForce)
          : null,
    });

  const submitRerunDraft = async () => {
    if (!rerunDraft) return;
    const { state, note, request, slurm } = rerunDraft;
    // Same rule as the approval gate: patch the resources only if the researcher
    // actually retyped one, compared against the figures the form was seeded with
    // -- which are the ones in force, not the plan's original proposal. Omitting
    // them leaves the approved plan alone (the API treats a missing slurm_request
    // as "no change"), where sending an untouched echo overwrote the researcher's
    // approved numbers with TWAIN's and called the revert "your edited resource
    // request" in the transcript.
    const rerunSeed = slurmInForce ? seedSlurmDraft(slurmInForce) : null;
    const slurmRetyped = !!slurm && !!rerunSeed && !sameSlurmDraft(slurm, rerunSeed);
    await handleRerun(
      state,
      request?.trim() || undefined,
      note.trim() || undefined,
      slurm && slurmRetyped
        ? clampSlurm(
            {
              cpu_count: Math.max(1, parseInt(slurm.cpu_count, 10) || 8),
              gpu_count: Math.max(0, parseInt(slurm.gpu_count, 10) || 0),
              ram: Math.max(MIN_RAM_GB, parseInt(slurm.ram, 10) || MIN_RAM_GB),
              max_time: Math.max(
                MIN_WALL_HOURS,
                durationToHours(slurm.max_time, slurm.max_time_unit)
                  ?? slurmInForce?.max_time
                  ?? 0.17,
              ),
            },
            slurmLimits,
          )
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
      {/* Same crimson-to-plum ramp as the app masthead. This screen draws its own
          top bar rather than using <Header>, so the gradient is repeated here --
          both read Gradients.brandHeader, so the two cannot drift apart. */}
      <LinearGradient
        colors={Gradients.brandHeader}
        locations={Gradients.brandHeaderLocations}
        start={{ x: 0, y: 0 }}
        end={{ x: 1, y: 1 }}
        style={styles.topBar}
      >
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
      </LinearGradient>

      {conversation && (
        <View style={styles.targetBadgeRow}>
          {/* All runs execute on the RIS cluster; the badge just confirms which one. */}
          <Text style={[styles.targetBadge, styles.targetBadgeSlurm]}>
            {`RIS / Slurm${approvalPlan?.slurm_cluster ? ` · ${approvalPlan.slurm_cluster}` : ''}`}
          </Text>
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
        {messages.map((m, i) => (
          <MessageBubble
            key={m.id}
            message={m}
            // The resources actually approved live in the response that FOLLOWS
            // this card, so they are looked up here rather than inside the card.
            approved={m.kind === 'approval_request' ? approvedAfter(messages, i) : null}
          />
        ))}
        {isActive && !awaitingApproval && (
          // Live running time sits beside the activity that explains it -- the
          // two answer one question together ("what is it doing, and for how
          // long?"). The checklist and job log come from the run's activity
          // events; with none yet it reads like the old spinner line.
          <RunActivity
            stage={conversation?.current_state ?? ''}
            activity={activity}
            now={now}
            runElapsed={runElapsed}
            preferFallback={cancelling || status === 'awaiting_input'}
            fallbackLabel={
              cancelling
                ? 'Terminating the run…'
                : status === 'awaiting_input'
                  ? 'Waiting for your answer…'
                  : 'Working…'
            }
          />
        )}
        {status === 'error' && activity.failure && (
          // Where it stopped and why -- instead of "see the run log", which
          // pointed at a log nothing here shows.
          <FailureCard failure={activity.failure} />
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
                    slurmLimits.max_time != null
                      ? `${formatDurationHours(slurmLimits.max_time)} wall` : null,
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
              </View>
              {/* Wall time gets the full row to itself: sharing a half-width
                  column with RAM clipped the unit buttons off the right edge of a
                  375pt phone, which is the width this card is most used at. */}
              <View style={styles.slurmRow}>
                {/* The MINIMUM is advertised, as RAM's is. Both fields are
                    floored on submit, and a floor the reader cannot see is worse
                    than no floor: a 5-minute entry came back as "Slurm ask
                    (yours) … 10m wall", attributing to the researcher a number
                    they did not choose and were never told about. */}
                <WallTimeField
                  label={wallTimeLabel(slurmLimits?.max_time)}
                  value={slurmDraft.max_time}
                  unit={slurmDraft.max_time_unit}
                  onChange={(v, u) =>
                    setSlurmDraft({ ...slurmDraft, max_time: v, max_time_unit: u })
                  }
                />
              </View>
            </View>
          )}

          {/* The acceptance bar. Separate from the resource block because it is a
              different kind of decision: resources are about what the run costs,
              this is about what counts as a right answer -- and it is the one
              figure TWAIN often cannot supply, writing null and leaving the result
              with nothing to be checked against. */}
          {metricDrafts.length > 0 && (
            <View style={styles.slurmEditor}>
              <Text style={styles.slurmEditorTitle}>
                {planProposedATarget
                  ? 'Accept if — suggested by TWAIN, editable'
                  : 'Accept if — TWAIN proposed no target'}
              </Text>
              <Text style={styles.slurmSuggestHint}>
                {planProposedATarget
                  ? 'The result is accepted when it lands within the tolerance of '
                    + 'the target. These are TWAIN’s figures — change them, or clear '
                    + 'both to judge against the literature alone.'
                  : 'TWAIN had no defensible expected value for this property, so '
                    + 'nothing will check the answer beyond the literature. If you '
                    + 'know roughly what to expect, set it here.'}
              </Text>
              {metricDrafts.map((draft, index) => (
                <View key={draft.metric_name} style={styles.metricRow}>
                  <Text style={styles.metricName} numberOfLines={1}>
                    {draft.metric_name}
                  </Text>
                  <View style={styles.slurmRow}>
                    <SlurmField
                      label={
                        seededMetricDrafts[index]?.target_value
                          ? 'Target value — suggested'
                          : 'Target value'
                      }
                      value={draft.target_value}
                      placeholder="none"
                      onChange={(v) => setMetricDraft(index, { target_value: v })}
                    />
                    <SlurmField
                      label={
                        seededMetricDrafts[index]?.tolerance
                          ? '± tolerance — suggested'
                          : '± tolerance'
                      }
                      value={draft.tolerance}
                      placeholder="none"
                      onChange={(v) => setMetricDraft(index, { tolerance: v })}
                    />
                  </View>
                </View>
              ))}
            </View>
          )}
          <View style={styles.approvalButtons}>
            {/* The one irreversible action here -- it spends cluster time -- so it
                is the only gradient-filled control on the screen, and the only one
                that answers a press physically. */}
            <PrimaryButton
              label="Approve & submit to RIS"
              onPress={() => handleApproval('approve')}
              disabled={busy}
              accessibilityLabel="Approve and submit to RIS"
            />
            <PressableScale
              style={[styles.rejectBtn, busy && styles.disabled]}
              onPress={() => handleApproval('reject')}
              accessibilityRole="button"
            >
              <Text style={styles.rejectText}>Reject</Text>
            </PressableScale>
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
            <PrimaryButton
              label="Yes, run it"
              onPress={() => handleQuickReply('yes')}
              disabled={busy}
            />
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
            <PrimaryButton
              label="Accept this result"
              onPress={() => handleQuickReply('accept')}
              disabled={busy}
            />
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
                      <WallTimeField
                        label={wallTimeLabel(slurmLimits?.max_time)}
                        value={rerunDraft.slurm.max_time}
                        unit={rerunDraft.slurm.max_time_unit}
                        onChange={(v, u) =>
                          setRerunDraft({
                            ...rerunDraft,
                            slurm: { ...rerunDraft.slurm!, max_time: v, max_time_unit: u },
                          })
                        }
                      />
                    </View>
                  )}
                </ScrollView>
                <View style={styles.approvalButtons}>
                  <PrimaryButton
                    label="Re-run with this"
                    onPress={submitRerunDraft}
                    disabled={
                      busy || (rerunDraft.request !== null && !rerunDraft.request.trim())
                    }
                  />
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
  /** Shown when the field is empty. An acceptance target is legitimately blank
      when TWAIN had no defensible value, and a bare empty box reads as a field
      that failed to load rather than as a deliberate "none". */
  placeholder?: string;
}> = ({ label, value, onChange, placeholder }) => (
  <View style={styles.slurmField}>
    <Text style={styles.slurmFieldLabel}>{label}</Text>
    <TextInput
      style={styles.slurmFieldInput}
      value={value}
      onChangeText={onChange}
      keyboardType="decimal-pad"
      placeholder={placeholder}
      placeholderTextColor={C.textPlaceholder}
      accessibilityLabel={label}
    />
  </View>
);

/**
 * Wall time: a number, plus a control that says what the number counts.
 *
 * The unit used to be typed into the value ("90m", "1.5h"), which is unreachable
 * on a phone -- a numeric field brings up a keypad with no letters, so the only
 * thing enterable there was a bare number, silently meaning HOURS. Asking for
 * "10 minutes" came out as 10 hours, or as 0.1666 if you did the division
 * yourself. Two controls, so neither the keyboard nor the reader has to guess.
 */
/**
 * The user's approval decision, in words.
 *
 * The stored content is the wire format the runner reads -- either a bare
 * "approve"/"reject" or `{"decision": …, "slurm_request": …}` with max_time in
 * HOURS, because that is the unit an sbatch takes. Rendering it verbatim showed
 * the researcher their own decision as raw JSON with `"max_time":
 * 0.16666666666666666` in it, which is the ten minutes they asked for wearing a
 * disguise. Units the plan needs are not units a person reads: convert here.
 */
function describeApproval(content: string): string {
  const decided = (word: string) => (word === 'reject' ? 'Rejected this plan.' : 'Approved this plan.');
  let parsed: { decision?: string; slurm_request?: Record<string, unknown> } | null = null;
  try {
    const candidate = JSON.parse(content);
    parsed = typeof candidate === 'object' && candidate ? candidate : null;
  } catch {
    parsed = null;
  }
  if (!parsed) return decided(String(content).trim().toLowerCase());
  const slurm = parsed.slurm_request;
  const head = decided(String(parsed.decision ?? '').trim().toLowerCase());
  if (!slurm) return head;
  const hours = typeof slurm.max_time === 'number' ? slurm.max_time : null;
  const parts = [
    slurm.cpu_count != null ? `${slurm.cpu_count} CPU` : null,
    // Only worth a mention when there are any -- every run asks for 0.
    slurm.gpu_count ? `${slurm.gpu_count} GPU` : null,
    slurm.ram != null ? `${slurm.ram} GB RAM` : null,
    hours != null && formatDurationHours(hours)
      ? `${formatDurationHours(hours)} wall time` : null,
  ].filter(Boolean);
  return parts.length ? `${head} Resources: ${parts.join(', ')}.` : head;
}

/**
 * The resource overrides from the approval_response that answered this card.
 *
 * A run can hold several approval cards (a re-run replans), so this takes the
 * FIRST response after the given index rather than the last in the conversation --
 * otherwise a later re-run's numbers would be attributed to an earlier plan.
 * Null when the card was never answered, or answered without edits.
 */
type Approved = {
  slurm: ApprovedResources | null;
  metrics: ApprovedMetric[] | null;
};

const NOTHING_APPROVED: Approved = { slurm: null, metrics: null };

function approvedAfter(messages: Message[], index: number): Approved {
  for (let i = index + 1; i < messages.length; i += 1) {
    const m = messages[i];
    if (m.kind === 'approval_request') return NOTHING_APPROVED;  // this card lapsed
    if (m.kind !== 'approval_response') continue;
    try {
      const parsed = JSON.parse(m.content);
      const slurm = parsed?.slurm_request;
      const metrics = parsed?.acceptance_metrics;
      return {
        slurm: slurm && typeof slurm === 'object' ? (slurm as ApprovedResources) : null,
        metrics: Array.isArray(metrics) ? (metrics as ApprovedMetric[]) : null,
      };
    } catch {
      return NOTHING_APPROVED;   // a bare "approve"/"reject": nothing overridden
    }
  }
  return NOTHING_APPROVED;
}

const MessageBubble: React.FC<{
  message: Message;
  approved?: Approved | null;
}> = ({ message, approved }) => {
  const isUser = message.role === 'user';
  if (message.kind === 'terminate') {
    return <Text style={styles.terminateNote}>You asked to terminate this run.</Text>;
  }
  if (message.kind === 'approval_request') {
    return (
      <PlanCard
        content={message.content}
        approved={approved?.slurm}
        approvedMetrics={approved?.metrics}
      />
    );
  }
  if (message.kind === 'approval_response') {
    return (
      <View style={[styles.bubble, styles.userBubble]}>
        <Text style={[styles.bubbleText, styles.userBubbleText]}>
          {describeApproval(message.content)}
        </Text>
      </View>
    );
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

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: C.background },
  topBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    borderBottomWidth: 2,
    borderBottomColor: 'rgba(33,87,50,0.9)',
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
    borderRadius: Radius.card,
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
  scroll: { flex: 1 },
  scrollContent: { padding: Spacing.three, gap: Spacing.two },
  hint: { color: C.textSecondary, fontSize: 15, lineHeight: 22, padding: Spacing.two },
  bubble: { maxWidth: '85%', borderRadius: 12, padding: Spacing.three },
  userBubble: { alignSelf: 'flex-end', backgroundColor: C.washuRed },
  assistantBubble: { alignSelf: 'flex-start', backgroundColor: C.backgroundElement },
  bubbleTag: { fontSize: 10, fontWeight: '700', color: C.washuGreen, marginBottom: 4 },
  bubbleText: { fontSize: 15, color: C.text, lineHeight: 21 },
  userBubbleText: { color: C.washuWhite },
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
    borderRadius: Radius.control,
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
    borderRadius: Radius.card,
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
    borderRadius: Radius.control,
    paddingHorizontal: Spacing.two,
    paddingVertical: Spacing.one,
    backgroundColor: C.washuWhite,
    fontSize: 14,
    color: C.text,
  },
  metricRow: { gap: Spacing.one, marginTop: Spacing.one },
  metricName: { fontSize: 13, fontWeight: '700', color: C.text },
  approvalButtons: { flexDirection: 'row', gap: Spacing.two },
  // The primary action's styles live in PrimaryButton, not here. They were here,
  // as a bare `approveBtn`, and four call sites shared them until a restyle split
  // the fill onto an inner gradient and only one call site followed.
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
    backgroundColor: Motion.scrimColor,
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
    borderRadius: Radius.control,
    borderWidth: 1,
    borderColor: C.border,
  },
  modalCancelText: { fontSize: 15, fontWeight: '600', color: C.text },
  disabled: { opacity: 0.5 },
});

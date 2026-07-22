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
import { apiClient, Conversation, Message } from '@/api/client';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

// The happy-path pipeline states shown in the stepper (loops CORRECT/REPLAN omitted).
const PIPELINE_STATES = [
  'INTAKE', 'CLARIFY', 'DECOMPOSE', 'DISCOVER', 'PLAN',
  'BUILD', 'EXECUTE', 'INTERPRET', 'VALIDATE', 'ACCEPT', 'TERMINATE',
];

// Stages a finished run can be restarted from, with plain-language descriptions
// of what re-running each one redoes. A re-run resets the chosen stage and every
// stage after it, keeping the earlier work as input.
const RERUN_STAGES: { state: string; label: string; desc: string }[] = [
  { state: 'INTAKE', label: 'Intake', desc: 'Re-read your request from scratch' },
  { state: 'CLARIFY', label: 'Clarify', desc: 'Re-ask the clarifying questions' },
  { state: 'DECOMPOSE', label: 'Decompose', desc: 'Rebuild the goal breakdown' },
  { state: 'DISCOVER', label: 'Discover', desc: 'Re-pick the candidate tools' },
  { state: 'PLAN', label: 'Plan', desc: 'Re-synthesize the execution plan' },
  { state: 'BUILD', label: 'Build', desc: 'Regenerate the run code' },
  { state: 'EXECUTE', label: 'Execute', desc: 'Re-run the calculation' },
];

const ACTIVE_STATUSES = ['running', 'awaiting_input', 'awaiting_approval'];
const TERMINAL_STATUSES = ['completed', 'error', 'rejected'];
const POLL_MS = 1500;

export const ChatScreen: React.FC = () => {
  const router = useRouter();
  const params = useLocalSearchParams<{ id?: string }>();
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [input, setInput] = useState('');
  const [budget, setBudget] = useState('');  // per-run cost cap (USD); blank => default
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rerunOpen, setRerunOpen] = useState(false);  // "Re-run from…" picker
  const scrollRef = useRef<ScrollView>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const conversationId = conversation?.id ?? (params.id as string | undefined);
  const status = conversation?.status;
  const isActive = !!status && ACTIVE_STATUSES.includes(status);
  const isTerminal = !!status && TERMINAL_STATUSES.includes(status);
  const terminalMessage =
    status === 'completed'
      ? '✓ Simulation complete — your results are ready.'
      : status === 'rejected'
        ? 'Plan rejected — nothing was executed.'
        : 'The run ended with an error.';

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
  }, [params.id, conversation]);

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

  const handleApproval = async (decision: 'approve' | 'reject') => {
    if (!conversation || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiClient.sendApproval(conversation.id, decision);
      await refresh(conversation.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to submit decision');
    } finally {
      setBusy(false);
    }
  };

  const handleRerun = async (state: string) => {
    if (!conversationId || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiClient.rerunConversation(conversationId, state);
      setRerunOpen(false);
      // Reload the full conversation (now `running` at `state`, with the marker
      // message); the poll effect restarts automatically once it's active again.
      await refresh(conversationId);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to re-run from that step');
    } finally {
      setBusy(false);
    }
  };

  const messages = conversation?.messages ?? [];
  const awaitingApproval = status === 'awaiting_approval';
  // The stages this run reached (so the picker only offers steps that ran).
  const reachedIndex = conversation ? PIPELINE_STATES.indexOf(conversation.current_state) : -1;

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
        <View style={{ width: 48 }} />
      </View>

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
              {status === 'awaiting_input' ? 'Waiting for your answer…' : 'Working…'}
            </Text>
          </View>
        )}
      </ScrollView>

      {error && <Text style={styles.error}>{error}</Text>}

      {awaitingApproval ? (
        <View style={styles.approvalBar}>
          <Text style={styles.approvalLabel}>Approve this plan?</Text>
          <View style={styles.approvalButtons}>
            <TouchableOpacity
              style={[styles.approveBtn, busy && styles.disabled]}
              onPress={() => handleApproval('approve')}
              accessibilityRole="button"
            >
              <Text style={styles.approveText}>Approve & run</Text>
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
        onRequestClose={() => (busy ? undefined : setRerunOpen(false))}
      >
        <View style={styles.modalBackdrop}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>Re-run from a step</Text>
            <Text style={styles.modalHint}>
              Pick a step to restart from. That step and everything after it run again; the
              earlier steps are kept.
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
                    onPress={() => handleRerun(stage.state)}
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
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
};

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

interface PlanSummary {
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
    tool_version?: number | string;
    calculator?: string;
    libraries?: string[];
  } | null;
  cost_estimate?: { min_cost?: number } | null;
  compute_estimate?: { cpu_hours?: number } | null;
  acceptance_metrics?: { metric_name?: string; target_value?: number; tolerance?: number }[] | null;
  safety_notes?: string[] | null;
}

// Renders the approval-gate plan: leads with the plain-language summary of what
// the run will do, then the concrete method / system / cost / notes.
const PlanCard: React.FC<{ content: string }> = ({ content }) => {
  let plan: PlanSummary | null = null;
  try {
    plan = JSON.parse(content) as PlanSummary;
  } catch {
    plan = null;
  }
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

  return (
    <View style={styles.planCard}>
      <Text style={styles.planTitle}>Proposed execution plan</Text>
      {plan.summary ? <Text style={styles.planSummary}>{plan.summary}</Text> : null}
      {sysText ? <PlanRow label="System" value={sysText} /> : null}
      {plan.requested_property ? <PlanRow label="Property" value={plan.requested_property} /> : null}
      {methodText ? (
        <PlanRow label="Method" value={libs ? `${methodText}  ·  ${libs}` : methodText} />
      ) : null}
      {costText ? <PlanRow label="Estimated cost" value={costText} /> : null}
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
  back: { color: '#FFFFFF', fontSize: 16, fontWeight: '600', width: 48 },
  title: { color: '#FFFFFF', fontSize: 16, fontWeight: '700', flex: 1, textAlign: 'center' },
  stepper: { maxHeight: 62, backgroundColor: C.backgroundElement, flexGrow: 0 },
  stepperContent: { alignItems: 'center', paddingHorizontal: Spacing.three, gap: Spacing.three },
  step: { alignItems: 'center', gap: 4, paddingVertical: Spacing.two },
  dot: { width: 12, height: 12, borderRadius: 6, backgroundColor: '#CCCCCC' },
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
  userBubbleText: { color: '#FFFFFF' },
  planCard: {
    alignSelf: 'stretch',
    borderRadius: 12,
    borderWidth: 1,
    borderColor: C.washuGreen,
    padding: Spacing.three,
    backgroundColor: C.washuWhite,
  },
  planTitle: { fontSize: 14, fontWeight: '700', color: C.washuGreen, marginBottom: Spacing.two },
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
    borderColor: '#DDDDDD',
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
    borderColor: '#DDDDDD',
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
  sendText: { color: '#FFFFFF', fontWeight: '700', fontSize: 15 },
  approvalBar: {
    padding: Spacing.three,
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
    gap: Spacing.two,
  },
  approvalLabel: { fontSize: 15, fontWeight: '600', color: C.text },
  approvalButtons: { flexDirection: 'row', gap: Spacing.two },
  approveBtn: {
    flex: 1,
    backgroundColor: C.washuGreen,
    borderRadius: 10,
    paddingVertical: Spacing.three,
    alignItems: 'center',
  },
  approveText: { color: '#FFFFFF', fontWeight: '700', fontSize: 15 },
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
  reportText: { color: '#FFFFFF', fontWeight: '700', fontSize: 15 },
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
    borderColor: '#DDDDDD',
  },
  modalCancelText: { fontSize: 15, fontWeight: '600', color: C.text },
  disabled: { opacity: 0.5 },
});

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
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

const ACTIVE_STATUSES = ['running', 'awaiting_input', 'awaiting_approval'];
const TERMINAL_STATUSES = ['completed', 'error', 'rejected'];
const POLL_MS = 1500;

export const ChatScreen: React.FC = () => {
  const router = useRouter();
  const params = useLocalSearchParams<{ id?: string }>();
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<ScrollView>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const conversationId = conversation?.id ?? (params.id as string | undefined);
  const status = conversation?.status;
  const isActive = !!status && ACTIVE_STATUSES.includes(status);
  const isTerminal = !!status && TERMINAL_STATUSES.includes(status);
  const terminalMessage =
    status === 'completed'
      ? 'Run complete.'
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
        const created = await apiClient.startConversation(text);
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

  const messages = conversation?.messages ?? [];
  const awaitingApproval = status === 'awaiting_approval';

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
          <TouchableOpacity
            style={styles.reportBtn}
            onPress={() =>
              router.push({ pathname: '/report', params: { id: conversationId as string } })
            }
            accessibilityRole="button"
          >
            <Text style={styles.reportText}>View report</Text>
          </TouchableOpacity>
        </View>
      ) : (
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
      )}
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
    return (
      <View style={styles.planCard}>
        <Text style={styles.planTitle}>Proposed execution plan</Text>
        <Text style={styles.planBody}>{prettyPlan(message.content)}</Text>
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

function prettyPlan(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

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
  working: { flexDirection: 'row', alignItems: 'center', gap: Spacing.two, padding: Spacing.two },
  workingText: { color: C.textSecondary, fontSize: 13 },
  error: { color: C.washuRed, paddingHorizontal: Spacing.three, paddingVertical: Spacing.one },
  inputBar: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: Spacing.two,
    padding: Spacing.two,
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
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
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: Spacing.two,
    padding: Spacing.three,
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
  },
  terminalText: { flex: 1, fontSize: 14, color: C.textSecondary },
  reportBtn: {
    backgroundColor: C.washuGreen,
    borderRadius: 10,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.three,
  },
  reportText: { color: '#FFFFFF', fontWeight: '700', fontSize: 15 },
  disabled: { opacity: 0.5 },
});

import React, { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Linking,
  Modal,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import {
  apiClient,
  IssueCategory,
  IssueContext,
  RunIssue,
} from '@/api/client';
import { Colors, Elevation, Radius, Spacing } from '@/constants/theme';

const C = Colors.light;

const CATEGORIES: { key: IssueCategory; label: string; hint: string }[] = [
  { key: 'bug', label: 'Bug', hint: 'The run broke, stalled, or behaved wrongly.' },
  { key: 'library', label: 'Library', hint: 'You need a library TWAIN does not have installed.' },
  { key: 'result', label: 'Result', hint: 'The run finished but the number looks wrong.' },
  { key: 'other', label: 'Other', hint: 'Anything else about this run.' },
];

const MAX_TITLE = 160;

interface ReportIssueModalProps {
  visible: boolean;
  /** The run being reported on; its data is attached to the issue server-side. */
  conversationId: string;
  onClose: () => void;
  /** Called with the filed issue so the run window can note it was reported. */
  onSubmitted?: (issue: RunIssue) => void;
}

/**
 * Report a problem with the *current run* without leaving TWAIN.
 *
 * Submitting publishes the run's data to the issue tracker, so the form shows
 * the exact snapshot that will be attached (collapsed by default) rather than
 * asking the user to take that on trust. The attachment is assembled by the API
 * from the run's own tables — nothing sensitive is gathered on the client.
 */
export const ReportIssueModal: React.FC<ReportIssueModalProps> = ({
  visible,
  conversationId,
  onClose,
  onSubmitted,
}) => (
  <Modal
    visible={visible}
    transparent
    animationType="fade"
    onRequestClose={onClose}
    accessibilityViewIsModal
  >
    <View style={styles.overlay}>
      {/* Mounted only while open, so every open starts from a clean form and a
          freshly fetched snapshot — no state to reset. */}
      {visible && (
        <IssueForm conversationId={conversationId} onClose={onClose} onSubmitted={onSubmitted} />
      )}
    </View>
  </Modal>
);

const IssueForm: React.FC<Omit<ReportIssueModalProps, 'visible'>> = ({
  conversationId,
  onClose,
  onSubmitted,
}) => {
  const [category, setCategory] = useState<IssueCategory>('bug');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [context, setContext] = useState<IssueContext | null>(null);
  const [showAttachment, setShowAttachment] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filed, setFiled] = useState<RunIssue | null>(null);

  // Fetch the snapshot on mount: a run keeps moving, so what would be attached
  // now is not what would have been attached a minute ago.
  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    (async () => {
      try {
        const loaded = await apiClient.getIssueContext(conversationId);
        if (!cancelled) setContext(loaded);
      } catch {
        // Not fatal — the user can still describe the problem, and the API
        // re-collects the snapshot when it files the issue.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  const handleSubmit = async () => {
    if (busy) return;
    if (!title.trim() || !description.trim()) {
      setError('Add a title and a short description of what happened.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const issue = await apiClient.submitRunIssue(conversationId, {
        category,
        title: title.trim(),
        description: description.trim(),
      });
      setFiled(issue);
      onSubmitted?.(issue);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not submit the report.');
    } finally {
      setBusy(false);
    }
  };

  const activeHint = CATEGORIES.find((c) => c.key === category)?.hint;

  return (
    <View style={styles.dialog}>
      <View style={styles.header}>
        <Text style={styles.headerTitle}>
          {filed ? 'Report submitted' : 'Report an issue with this run'}
        </Text>
      </View>

      <ScrollView style={styles.bodyScroll} contentContainerStyle={styles.body}>
        {filed ? (
          <SubmittedPanel issue={filed} />
        ) : (
          <>
            <Text style={styles.label}>Category</Text>
            <View style={styles.chips}>
              {CATEGORIES.map((c) => {
                const selected = c.key === category;
                return (
                  <TouchableOpacity
                    key={c.key}
                    style={[styles.chip, selected && styles.chipSelected]}
                    onPress={() => setCategory(c.key)}
                    accessibilityRole="button"
                    accessibilityState={{ selected }}
                  >
                    <Text style={[styles.chipText, selected && styles.chipTextSelected]}>
                      {c.label}
                    </Text>
                  </TouchableOpacity>
                );
              })}
            </View>
            {activeHint && <Text style={styles.hint}>{activeHint}</Text>}

            <Text style={styles.label}>Title</Text>
            <TextInput
              style={styles.input}
              value={title}
              onChangeText={setTitle}
              placeholder="Short summary, e.g. “run stalled at EXECUTE”"
              placeholderTextColor={C.textSecondary}
              maxLength={MAX_TITLE}
              editable={!busy}
            />

            <Text style={styles.label}>What happened?</Text>
            <TextInput
              style={[styles.input, styles.textArea]}
              value={description}
              onChangeText={setDescription}
              placeholder="What you expected, what you saw instead, anything you tried."
              placeholderTextColor={C.textSecondary}
              editable={!busy}
              multiline
            />

            <AttachmentDisclosure
              context={context}
              expanded={showAttachment}
              onToggle={() => setShowAttachment((v) => !v)}
            />

            {context && !context.github_configured && (
              <Text style={styles.warning}>
                This deployment has no issue-tracker credentials, so your report will be saved
                against the run but no GitHub issue will be filed.
              </Text>
            )}
          </>
        )}
        {error && <Text style={styles.error}>{error}</Text>}
      </ScrollView>

      <View style={styles.footer}>
        {filed ? (
          <TouchableOpacity style={styles.primaryBtn} onPress={onClose} accessibilityRole="button">
            <Text style={styles.primaryText}>Done</Text>
          </TouchableOpacity>
        ) : (
          <>
            <TouchableOpacity
              style={styles.secondaryBtn}
              onPress={onClose}
              accessibilityRole="button"
            >
              <Text style={styles.secondaryText}>Cancel</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.primaryBtn, busy && styles.disabled]}
              onPress={handleSubmit}
              accessibilityRole="button"
              accessibilityLabel="Submit issue"
            >
              {busy ? (
                <ActivityIndicator color={C.washuWhite} />
              ) : (
                <Text style={styles.primaryText}>Submit issue</Text>
              )}
            </TouchableOpacity>
          </>
        )}
      </View>
    </View>
  );
};

/** What the API will attach, shown in full so consent is informed. */
const AttachmentDisclosure: React.FC<{
  context: IssueContext | null;
  expanded: boolean;
  onToggle: () => void;
}> = ({ context, expanded, onToggle }) => {
  const run = context?.run_context;
  const summary = run
    ? [
        `run id ${run.run_id}`,
        run.current_state ? `state ${run.current_state}` : null,
        run.selected_method?.libraries?.length
          ? `toolset ${run.selected_method.libraries.join(' + ')}`
          : null,
        run.errors.length ? `${run.errors.length} error(s)` : null,
        run.recent_messages.length ? `${run.recent_messages.length} recent message(s)` : null,
        run.artifacts.length ? `${run.artifacts.length} file name(s)` : null,
      ]
        .filter(Boolean)
        .join(', ')
    : 'loading…';

  return (
    <View style={styles.attachment}>
      <TouchableOpacity onPress={onToggle} accessibilityRole="button">
        <Text style={styles.attachmentToggle}>
          {expanded ? '▾' : '▸'} Run data attached to this report
        </Text>
      </TouchableOpacity>
      <Text style={styles.attachmentSummary}>{summary}</Text>
      {expanded && run && (
        <ScrollView style={styles.attachmentBody} nestedScrollEnabled>
          <Text style={styles.mono}>{JSON.stringify(run, null, 2)}</Text>
        </ScrollView>
      )}
      {expanded && run?.artifacts?.length ? (
        <Text style={styles.hint}>
          File names are attached, not their contents — a maintainer fetches those by run id.
        </Text>
      ) : null}
    </View>
  );
};

const SubmittedPanel: React.FC<{ issue: RunIssue }> = ({ issue }) => {
  if (issue.status === 'created' && issue.issue_url) {
    return (
      <View style={styles.result}>
        <Text style={styles.resultText}>
          Filed as issue #{issue.issue_number} with this run’s data attached.
        </Text>
        <TouchableOpacity
          onPress={() => Linking.openURL(issue.issue_url as string)}
          accessibilityRole="link"
        >
          <Text style={styles.link}>{issue.issue_url}</Text>
        </TouchableOpacity>
      </View>
    );
  }
  return (
    <View style={styles.result}>
      <Text style={styles.resultText}>
        {issue.status === 'queued'
          ? 'Your report was saved against this run. No GitHub issue was filed because this deployment has no issue-tracker credentials.'
          : 'Your report was saved against this run, but filing the GitHub issue failed.'}
      </Text>
      {issue.error && <Text style={styles.hint}>{issue.error}</Text>}
    </View>
  );
};

const styles = StyleSheet.create({
  overlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.55)',
    justifyContent: 'center',
    alignItems: 'center',
    padding: Spacing.four,
  },
  dialog: {
    backgroundColor: C.washuWhite,
    borderRadius: Radius.control,
    width: Platform.OS === 'web' ? 520 : '100%',
    maxWidth: 560,
    maxHeight: '90%',
    overflow: 'hidden',
    boxShadow: Elevation.card,
  },
  header: {
    backgroundColor: C.washuRed,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.three,
  },
  headerTitle: { color: C.washuWhite, fontSize: 16, fontWeight: '700', letterSpacing: 0.3 },
  bodyScroll: { flexGrow: 0 },
  body: { paddingHorizontal: Spacing.four, paddingVertical: Spacing.four, gap: Spacing.two },
  label: { fontSize: 13, fontWeight: '700', color: C.text, marginTop: Spacing.two },
  hint: { fontSize: 12, color: C.textSecondary, lineHeight: 17 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: Spacing.two },
  chip: {
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 16,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.one,
    backgroundColor: C.washuWhite,
  },
  chipSelected: { borderColor: C.washuRed, backgroundColor: C.backgroundSelected },
  chipText: { fontSize: 13, color: C.textSecondary, fontWeight: '600' },
  chipTextSelected: { color: C.washuRed },
  input: {
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: Radius.control,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    fontSize: 15,
    color: C.text,
    backgroundColor: C.washuWhite,
    minHeight: 44,
  },
  textArea: { minHeight: 96, maxHeight: 160, textAlignVertical: 'top' },
  attachment: {
    marginTop: Spacing.two,
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
    paddingTop: Spacing.two,
    gap: Spacing.one,
  },
  attachmentToggle: { fontSize: 13, fontWeight: '700', color: C.washuGreen },
  attachmentSummary: { fontSize: 12, color: C.textSecondary },
  attachmentBody: {
    maxHeight: 180,
    backgroundColor: C.backgroundElement,
    borderRadius: Radius.card,
    padding: Spacing.two,
  },
  mono: {
    fontSize: 11,
    color: C.text,
    fontFamily: Platform.select({ ios: 'Courier', android: 'monospace', default: 'monospace' }),
  },
  warning: { fontSize: 12, color: C.washuRed, lineHeight: 17, marginTop: Spacing.one },
  error: { fontSize: 13, color: C.washuRed, marginTop: Spacing.two },
  result: { gap: Spacing.two },
  resultText: { fontSize: 14, color: C.text, lineHeight: 20 },
  link: { fontSize: 13, color: C.washuGreen, textDecorationLine: 'underline' },
  footer: {
    flexDirection: 'row',
    justifyContent: 'flex-end',
    gap: Spacing.two,
    borderTopWidth: 1,
    borderTopColor: C.backgroundElement,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.three,
  },
  primaryBtn: {
    backgroundColor: C.washuRed,
    borderRadius: Radius.card,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    minWidth: 120,
    minHeight: 38,
    alignItems: 'center',
    justifyContent: 'center',
  },
  primaryText: { color: C.washuWhite, fontSize: 14, fontWeight: '700' },
  secondaryBtn: {
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: Radius.card,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    minHeight: 38,
    alignItems: 'center',
    justifyContent: 'center',
  },
  secondaryText: { color: C.textSecondary, fontSize: 14, fontWeight: '600' },
  disabled: { opacity: 0.6 },
});

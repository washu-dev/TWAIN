import React, { useState } from 'react';
import {
  Modal,
  View,
  Text,
  TextInput,
  TouchableOpacity,
  ActivityIndicator,
  StyleSheet,
  Platform,
  Linking,
} from 'react-native';
import { Colors, Spacing } from '@/constants/theme';
import type { CreatedIssue } from '@/api/client';

const C = Colors.light;

interface IssueModalProps {
  visible: boolean;
  /** Email shown to the user so they know how the issue will be attributed. */
  submitterEmail?: string;
  /** Pre-fill the form (e.g. a "provision this engine" request from a plan card). */
  initialTitle?: string;
  initialBody?: string;
  /** Performs the actual create call; resolves to the created issue. */
  onSubmit: (title: string, body: string) => Promise<CreatedIssue>;
  onClose: () => void;
}

export const IssueModal: React.FC<IssueModalProps> = ({
  visible,
  submitterEmail,
  initialTitle,
  initialBody,
  onSubmit,
  onClose,
}) => {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedIssue | null>(null);

  // Seed the form each time the modal opens (closing always clears it, so a
  // caller-provided draft must be re-applied on the next open). Done as a
  // render-time state adjustment -- the sanctioned alternative to setState in
  // an effect (react-hooks/set-state-in-effect); React re-renders immediately
  // without painting the stale frame.
  const [wasVisible, setWasVisible] = useState(visible);
  if (visible !== wasVisible) {
    setWasVisible(visible);
    if (visible) {
      setTitle(initialTitle ?? '');
      setBody(initialBody ?? '');
    }
  }

  const canSubmit = title.trim().length > 0 && !submitting;

  // Clear the form and dismiss. Used by every close path (Cancel, Done, and the
  // OS back/Esc gesture) so the next open starts clean without a syncing effect.
  const resetAndClose = () => {
    setTitle('');
    setBody('');
    setError(null);
    setCreated(null);
    setSubmitting(false);
    onClose();
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const issue = await onSubmit(title.trim(), body.trim());
      setCreated(issue);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Unknown error';
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      onRequestClose={resetAndClose}
      accessibilityViewIsModal
    >
      <View style={styles.overlay}>
        <View style={styles.dialog}>
          <View style={styles.header}>
            <Text style={styles.title}>Create New Issue</Text>
          </View>

          {created ? (
            // ── Success ──────────────────────────────────────────────────────
            <View style={styles.body}>
              <Text style={styles.successText}>
                Issue #{created.number} created in {created.repo}.
              </Text>
              <TouchableOpacity
                onPress={() => Linking.openURL(created.url)}
                accessibilityRole="link"
                accessibilityLabel={`Open issue ${created.number} on GitHub`}
              >
                <Text style={styles.link}>View it on GitHub →</Text>
              </TouchableOpacity>
            </View>
          ) : (
            // ── Form ──────────────────────────────────────────────────────────
            <View style={styles.body}>
              <Text style={styles.label}>Title</Text>
              <TextInput
                style={styles.input}
                value={title}
                onChangeText={setTitle}
                placeholder="Short summary of the issue"
                placeholderTextColor={C.textPlaceholder}
                editable={!submitting}
                maxLength={256}
                accessibilityLabel="Issue title"
              />

              <Text style={styles.label}>Description</Text>
              <TextInput
                style={[styles.input, styles.textArea]}
                value={body}
                onChangeText={setBody}
                placeholder="Steps to reproduce, expected vs. actual behavior, etc."
                placeholderTextColor={C.textPlaceholder}
                editable={!submitting}
                multiline
                numberOfLines={5}
                textAlignVertical="top"
                accessibilityLabel="Issue description"
              />

              {submitterEmail ? (
                <Text style={styles.note}>
                  Submitting as {submitterEmail}. Your email is recorded in the
                  issue, which is public.
                </Text>
              ) : null}

              {error ? <Text style={styles.errorText}>{error}</Text> : null}
            </View>
          )}

          {/* Footer */}
          <View style={styles.footer}>
            {created ? (
              <TouchableOpacity
                style={styles.primaryButton}
                onPress={resetAndClose}
                accessibilityRole="button"
                accessibilityLabel="Done"
              >
                <Text style={styles.primaryButtonText}>Done</Text>
              </TouchableOpacity>
            ) : (
              <>
                <TouchableOpacity
                  style={styles.secondaryButton}
                  onPress={resetAndClose}
                  disabled={submitting}
                  accessibilityRole="button"
                  accessibilityLabel="Cancel"
                >
                  <Text style={styles.secondaryButtonText}>Cancel</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={[styles.primaryButton, !canSubmit && styles.disabled]}
                  onPress={handleSubmit}
                  disabled={!canSubmit}
                  accessibilityRole="button"
                  accessibilityLabel="Submit issue"
                >
                  {submitting ? (
                    <ActivityIndicator size="small" color={C.washuWhite} />
                  ) : (
                    <Text style={styles.primaryButtonText}>Submit</Text>
                  )}
                </TouchableOpacity>
              </>
            )}
          </View>
        </View>
      </View>
    </Modal>
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
    backgroundColor: C.background,
    borderRadius: 8,
    width: Platform.OS === 'web' ? 480 : '100%',
    maxWidth: 520,
    overflow: 'hidden',
    shadowColor: C.shadow,
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.25,
    shadowRadius: 12,
    elevation: 8,
  },
  header: {
    backgroundColor: C.washuRed,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.three,
  },
  title: {
    color: C.washuWhite,
    fontSize: 16,
    fontWeight: '700',
    letterSpacing: 0.3,
  },
  body: {
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.four,
    gap: Spacing.two,
  },
  label: {
    fontSize: 13,
    fontWeight: '600',
    color: C.washuDarkGray,
    marginTop: Spacing.two,
  },
  input: {
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 6,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    fontSize: 15,
    color: C.textStrong,
    backgroundColor: C.background,
  },
  textArea: {
    minHeight: 110,
  },
  note: {
    fontSize: 12,
    color: C.textSecondary,
    marginTop: Spacing.two,
    lineHeight: 17,
  },
  successText: {
    fontSize: 15,
    color: C.textStrong,
    lineHeight: 22,
  },
  link: {
    fontSize: 15,
    fontWeight: '600',
    color: C.washuRed,
    marginTop: Spacing.two,
  },
  errorText: {
    fontSize: 13,
    color: C.washuRed,
    marginTop: Spacing.two,
  },
  footer: {
    borderTopWidth: 1,
    borderTopColor: C.divider,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.three,
    flexDirection: 'row',
    justifyContent: 'flex-end',
    gap: Spacing.two,
  },
  primaryButton: {
    backgroundColor: C.washuRed,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    borderRadius: 4,
    minWidth: 90,
    alignItems: 'center',
    justifyContent: 'center',
  },
  primaryButtonText: {
    color: C.washuWhite,
    fontSize: 14,
    fontWeight: '700',
  },
  secondaryButton: {
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    borderRadius: 4,
    borderWidth: 1,
    borderColor: C.borderStrong,
    minWidth: 90,
    alignItems: 'center',
    justifyContent: 'center',
  },
  secondaryButtonText: {
    color: C.washuDarkGray,
    fontSize: 14,
    fontWeight: '600',
  },
  disabled: {
    opacity: 0.5,
  },
});

import React from 'react';
import {
  Modal,
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  Platform,
} from 'react-native';
import { Colors, Elevation, Motion, Radius, Spacing } from '@/constants/theme';

const C = Colors.light;

interface MessageModalProps {
  visible: boolean;
  title: string;
  messages: string[];
  onClose: () => void;
}

export const MessageModal: React.FC<MessageModalProps> = ({
  visible,
  title,
  messages,
  onClose,
}) => {
  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      onRequestClose={onClose}
      accessibilityViewIsModal
    >
      <View style={styles.overlay}>
        <View style={styles.dialog}>
          {/* Header */}
          <View style={styles.header}>
            <Text style={styles.title}>{title}</Text>
          </View>

          {/* Body */}
          <View style={styles.body}>
            {messages.length === 0 ? (
              <Text style={styles.emptyText}>No messages found.</Text>
            ) : (
              messages.map((msg, i) => (
                <View key={i} style={styles.messageRow}>
                  <View style={styles.bullet} />
                  <Text style={styles.messageText}>{msg}</Text>
                </View>
              ))
            )}
          </View>

          {/* Footer */}
          <View style={styles.footer}>
            <TouchableOpacity
              style={styles.closeButton}
              onPress={onClose}
              accessibilityRole="button"
              accessibilityLabel="Close"
            >
              <Text style={styles.closeButtonText}>Close</Text>
            </TouchableOpacity>
          </View>
        </View>
      </View>
    </Modal>
  );
};

const styles = StyleSheet.create({
  overlay: {
    flex: 1,
    backgroundColor: Motion.scrimColor,
    justifyContent: 'center',
    alignItems: 'center',
    padding: Spacing.four,
  },
  dialog: {
    backgroundColor: C.background,
    borderRadius: Radius.control,
    width: Platform.OS === 'web' ? 420 : '100%',
    maxWidth: 480,
    overflow: 'hidden',
    boxShadow: Elevation.card,
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
  messageRow: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: Spacing.two,
    paddingVertical: Spacing.one,
  },
  bullet: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: C.washuRed,
    marginTop: 5,
  },
  messageText: {
    flex: 1,
    fontSize: 15,
    color: C.textStrong,
    lineHeight: 22,
  },
  emptyText: {
    fontSize: 14,
    color: C.textSecondary,
    fontStyle: 'italic',
  },
  footer: {
    borderTopWidth: 1,
    borderTopColor: C.divider,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.three,
    alignItems: 'flex-end',
  },
  closeButton: {
    backgroundColor: C.washuRed,
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    borderRadius: 4,
  },
  closeButtonText: {
    color: C.washuWhite,
    fontSize: 14,
    fontWeight: '600',
  },
});

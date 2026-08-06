import React from 'react';
import { Text, TouchableOpacity, StyleSheet } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

interface IconTileProps {
  /** Short label, one or two words. This is the tile's whole message. */
  label: string;
  /** A text glyph, not an image: the app ships no icon font, and adding one for
      three tiles would be a dependency for decoration. `︎` on the caller's
      glyph asks for text presentation so it renders as a mark, not colour emoji. */
  glyph: string;
  /** Read by a screen reader in place of the glyph, which carries no meaning to it. */
  hint?: string;
  onPress?: () => void;
}

/**
 * A minor destination, at the weight a minor destination deserves.
 *
 * The homepage gave five things one size, so a reference page competed with the
 * primary action and the whole viewport was a flat menu. These sit three-across
 * in one row: reachable, and visibly not the point of the screen.
 */
export const IconTile: React.FC<IconTileProps> = ({ label, glyph, hint, onPress }) => (
  <TouchableOpacity
    style={styles.container}
    onPress={onPress}
    activeOpacity={0.75}
    accessible
    accessibilityLabel={label}
    accessibilityHint={hint}
    accessibilityRole="button"
  >
    <Text style={styles.glyph} aria-hidden accessibilityElementsHidden importantForAccessibility="no">
      {glyph}
    </Text>
    <Text style={styles.label} numberOfLines={1}>
      {label}
    </Text>
  </TouchableOpacity>
);

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: Spacing.one,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.one,
    backgroundColor: C.background,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: C.border,
    // 44pt is the smallest comfortable touch target; the padding above clears it
    // at every text size, so a tile never becomes decoration you cannot press.
    minHeight: 64,
  },
  glyph: {
    fontSize: 20,
    lineHeight: 24,
    color: C.textSecondary,
  },
  label: {
    fontSize: 12,
    fontWeight: '600',
    color: C.textSecondary,
    textAlign: 'center',
  },
});

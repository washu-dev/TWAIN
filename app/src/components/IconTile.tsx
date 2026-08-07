import React from 'react';
import { Text, StyleSheet } from 'react-native';
import { Colors, Elevation, Radius, Spacing } from '@/constants/theme';
import { PressableScale } from './Motion';

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
  <PressableScale
    style={styles.container}
    onPress={onPress}
    accessible
    accessibilityLabel={label}
    accessibilityHint={hint}
    accessibilityRole="button"
  >
    <Text style={styles.glyph} aria-hidden accessibilityElementsHidden importantForAccessibility="no">
      {glyph}
    </Text>
    {/* Two lines: at a third of a 375pt screen "Report an issue" does not fit on
        one at this size, and it truncated to "Report an is...". Wrapping keeps the
        label honest; every tile is the same height, so nothing shifts. */}
    <Text style={styles.label} numberOfLines={2}>
      {label}
    </Text>
  </PressableScale>
);

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: Spacing.two,
    paddingVertical: Spacing.four,
    paddingHorizontal: Spacing.one,
    backgroundColor: C.background,
    borderRadius: Radius.card,
    borderWidth: 1,
    borderColor: 'rgba(26,6,12,0.06)',
    boxShadow: Elevation.card,
    // Comfortably past the 44pt touch minimum. "Minor" is about rank on the page,
    // not about being small enough to miss with a thumb.
    minHeight: 96,
  },
  glyph: {
    fontSize: 28,
    lineHeight: 32,
    color: C.textSecondary,
  },
  label: {
    fontSize: 13,
    fontWeight: '600',
    color: C.textSecondary,
    textAlign: 'center',
  },
});

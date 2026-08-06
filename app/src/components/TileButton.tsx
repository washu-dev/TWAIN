import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

interface TileButtonProps {
  title: string;
  description: string;
  onPress?: () => void;
  accentColor?: string;
  /** `hero` for the one action the screen exists for: same tile, more weight, so
      the hierarchy is visible before anything is read. Everything else is a
      `row`, and the genuinely minor destinations are an IconTile instead. */
  variant?: 'hero' | 'row';
}

export const TileButton: React.FC<TileButtonProps> = ({
  title,
  description,
  onPress,
  accentColor = C.washuRed,
  variant = 'row',
}) => {
  const hero = variant === 'hero';
  return (
    <TouchableOpacity
      style={[styles.container, { borderLeftColor: accentColor }]}
      onPress={onPress}
      activeOpacity={0.75}
      accessible={true}
      accessibilityLabel={title}
      accessibilityHint={description}
      accessibilityRole="button"
    >
      <View
        style={[styles.accentBar, hero && styles.accentBarHero, { backgroundColor: accentColor }]}
      />

      <View style={[styles.contentContainer, hero && styles.contentContainerHero]}>
        <Text style={[styles.title, hero && styles.titleHero]}>{title}</Text>
        <Text style={[styles.description, hero && styles.descriptionHero]}>{description}</Text>
      </View>

      <Text style={[styles.arrow, hero && styles.arrowHero, { color: accentColor }]}>→</Text>
    </TouchableOpacity>
  );
};

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: C.background,
    marginBottom: Spacing.three,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: C.border,
    overflow: 'hidden',
    shadowColor: C.shadow,
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.08,
    shadowRadius: 4,
    elevation: 2,
  },
  accentBar: {
    width: 6,
    alignSelf: 'stretch',
  },
  accentBarHero: {
    width: 8,
  },
  contentContainer: {
    flex: 1,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.three,
  },
  title: {
    fontSize: 17,
    fontWeight: '700',
    color: C.textStrong,
    marginBottom: Spacing.one,
  },
  description: {
    fontSize: 13,
    color: C.textSecondary,
    lineHeight: 18,
  },
  arrow: {
    fontSize: 22,
    paddingRight: Spacing.three,
  },
  contentContainerHero: {
    paddingVertical: Spacing.four,
  },
  titleHero: {
    fontSize: 22,
  },
  descriptionHero: {
    fontSize: 14,
    lineHeight: 20,
  },
  arrowHero: {
    fontSize: 26,
  },
});

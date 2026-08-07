import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { Colors, Elevation, Gradients, Radius, Spacing } from '@/constants/theme';
import { PressableScale } from './Motion';

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
  // The accent bar becomes a gradient: the hero gets the action ramp, everything
  // else the calm one. A flat 6pt stripe is the single most dated element on the
  // old card; the same stripe with a vertical fade reads as an edge-lit surface.
  const accentRamp = hero ? Gradients.action : Gradients.calm;
  return (
    <PressableScale
      style={[styles.container, hero && styles.containerHero]}
      onPress={onPress}
      accessible
      accessibilityLabel={title}
      accessibilityHint={description}
      accessibilityRole="button"
    >
      <LinearGradient
        colors={accentRamp}
        start={{ x: 0, y: 0 }}
        end={{ x: 0, y: 1 }}
        style={[styles.accentBar, hero && styles.accentBarHero]}
      />

      <View style={[styles.contentContainer, hero && styles.contentContainerHero]}>
        <Text style={[styles.title, hero && styles.titleHero]}>{title}</Text>
        <Text style={[styles.description, hero && styles.descriptionHero]}>{description}</Text>
      </View>

      <Text style={[styles.arrow, hero && styles.arrowHero, { color: accentColor }]}>→</Text>
    </PressableScale>
  );
};

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: C.background,
    marginBottom: Spacing.three,
    borderRadius: Radius.card,
    borderWidth: 1,
    // A hairline that is almost the surface colour: the shadow does the
    // separating, and a visible 1px grey box on top of a shadow reads as two
    // competing edges.
    borderColor: 'rgba(26,6,12,0.06)',
    overflow: 'hidden',
    // Two layers, tinted with the brand ink rather than neutral black.
    boxShadow: Elevation.card,
  },
  containerHero: {
    borderRadius: Radius.hero,
    boxShadow: Elevation.hero,
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
    paddingVertical: Spacing.five,
  },
  titleHero: {
    fontSize: 24,
  },
  descriptionHero: {
    fontSize: 14,
    lineHeight: 20,
  },
  arrowHero: {
    fontSize: 26,
  },
});

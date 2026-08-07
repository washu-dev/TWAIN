import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { APP_STRINGS, Colors, Elevation, Gradients, Radius, Spacing } from '@/constants/theme';
import { PressableScale } from './Motion';

const C = Colors.light;

interface HeaderProps {
  onLoginPress?: () => void;
  loginLabel?: string;
}

export const Header: React.FC<HeaderProps> = ({
  onLoginPress,
  loginLabel = APP_STRINGS.loginButton,
}) => {
  return (
    // Crimson into plum on the diagonal. The wordmark sits over the darkest
    // corner and the light edge lands where content begins, so the masthead
    // reads as one lit surface rather than a flat colour band.
    <LinearGradient
      colors={Gradients.brandHeader}
      locations={Gradients.brandHeaderLocations}
      start={{ x: 0, y: 0 }}
      end={{ x: 1, y: 1 }}
      style={styles.container}
    >
      <View style={styles.content}>
        {/* Shield + Title */}
        <View style={styles.brand}>
          <View style={styles.titleContainer}>
            <Text style={styles.appTitle}>{APP_STRINGS.appTitle}</Text>
            <Text style={styles.appSubtitle}>{APP_STRINGS.appSubtitle}</Text>
          </View>
        </View>

        {/* Just the one button. The API health check that used to sit here is a
            developer tool; it is in Settings now, so this row holds only what a
            researcher needs from every screen. */}
        <View style={styles.buttonContainer}>
          <PressableScale
            style={styles.buttonSolid}
            onPress={onLoginPress}
            accessible
            accessibilityLabel={loginLabel}
            accessibilityRole="button"
          >
            <Text style={styles.buttonSolidText}>{loginLabel}</Text>
          </PressableScale>
        </View>
      </View>
    </LinearGradient>
  );
};

const styles = StyleSheet.create({
  container: {
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.four,
    // A 2pt green hairline instead of the old 4pt slab: at this weight the
    // secondary brand colour reads as a considered detail rather than a stripe.
    borderBottomWidth: 2,
    borderBottomColor: 'rgba(33,87,50,0.9)',
  },
  content: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: Spacing.three,
  },
  brand: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    flex: 1,
  },
  titleContainer: {
    flex: 1,
  },
  appTitle: {
    fontSize: 28,
    fontWeight: '700',
    color: C.washuWhite,
    // Wider tracking on a short all-caps wordmark. This one change does more for
    // "considered" than any amount of colour work.
    letterSpacing: 1.6,
  },
  appSubtitle: {
    fontSize: 12,
    color: 'rgba(255,255,255,0.85)',
    lineHeight: 17,
    marginTop: 3,
  },
  buttonContainer: {
    flexDirection: 'row',
    gap: Spacing.two,
  },
  buttonSolid: {
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    borderRadius: Radius.control,
    backgroundColor: C.background,
    minWidth: 80,
    alignItems: 'center',
    boxShadow: Elevation.onDark,
  },
  buttonSolidText: {
    color: C.washuRed,
    fontSize: 14,
    fontWeight: '700',
  },
});

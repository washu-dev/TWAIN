import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { APP_STRINGS, Colors, Spacing } from '@/constants/theme';

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
    <View style={styles.container}>
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
          <TouchableOpacity
            style={styles.buttonSolid}
            onPress={onLoginPress}
            accessible={true}
            accessibilityLabel={loginLabel}
            accessibilityRole="button"
          >
            <Text style={styles.buttonSolidText}>{loginLabel}</Text>
          </TouchableOpacity>
        </View>
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    backgroundColor: C.washuRed,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.four,
    borderBottomWidth: 4,
    borderBottomColor: C.washuGreen,
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
    fontWeight: 'bold',
    color: C.washuWhite,
    letterSpacing: 0.5,
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
    borderRadius: 4,
    backgroundColor: C.background,
    minWidth: 80,
    alignItems: 'center',
  },
  buttonSolidText: {
    color: C.washuRed,
    fontSize: 14,
    fontWeight: '700',
  },
});

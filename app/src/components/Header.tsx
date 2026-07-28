import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { APP_STRINGS, Spacing } from '@/constants/theme';

interface HeaderProps {
  onTestPress?: () => void;
  onLoginPress?: () => void;
  loginLabel?: string;
}

export const Header: React.FC<HeaderProps> = ({
  onTestPress,
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

        {/* Buttons */}
        <View style={styles.buttonContainer}>
          <TouchableOpacity
            style={styles.buttonOutline}
            onPress={onTestPress}
            accessible={true}
            accessibilityLabel={APP_STRINGS.testButton}
            accessibilityRole="button"
          >
            <Text style={styles.buttonOutlineText}>{APP_STRINGS.testButton}</Text>
          </TouchableOpacity>

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
    backgroundColor: '#BA0C2F',
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.four,
    borderBottomWidth: 4,
    borderBottomColor: '#215732',
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
    color: '#FFFFFF',
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
  buttonOutline: {
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    borderRadius: 4,
    borderWidth: 2,
    borderColor: '#FFFFFF',
    minWidth: 80,
    alignItems: 'center',
  },
  buttonOutlineText: {
    color: '#FFFFFF',
    fontSize: 14,
    fontWeight: '600',
  },
  buttonSolid: {
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    borderRadius: 4,
    backgroundColor: '#FFFFFF',
    minWidth: 80,
    alignItems: 'center',
  },
  buttonSolidText: {
    color: '#BA0C2F',
    fontSize: 14,
    fontWeight: '700',
  },
});

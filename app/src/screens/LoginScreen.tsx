import React from 'react';
import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { APP_STRINGS, Colors, MaxContentWidth, Radius, Spacing } from '@/constants/theme';
import { useAuth } from '@/hooks/useAuth';

const C = Colors.light;

export const LoginScreen: React.FC = () => {
  const { signIn, isSigningIn, canSignIn, authConfigured, error } = useAuth();
  const router = useRouter();

  const disabled = isSigningIn || !canSignIn;

  return (
    <SafeAreaView style={styles.container}>
      <View style={styles.card}>
        <TouchableOpacity
          onPress={() => router.replace('/')}
          accessibilityRole="link"
          accessibilityLabel={APP_STRINGS.loginBackToHome}
        >
          <Text style={styles.backLink}>{APP_STRINGS.loginBackToHome}</Text>
        </TouchableOpacity>
        <Text style={styles.title}>{APP_STRINGS.appTitle}</Text>
        <Text style={styles.subtitle}>{APP_STRINGS.loginPrompt}</Text>

        {authConfigured ? (
          <>
            <TouchableOpacity
              style={[styles.button, disabled && styles.buttonDisabled]}
              onPress={signIn}
              disabled={disabled}
              accessibilityRole="button"
              accessibilityLabel={APP_STRINGS.ssoButton}
            >
              {isSigningIn ? (
                <ActivityIndicator color={C.washuWhite} />
              ) : (
                <Text style={styles.buttonText}>{APP_STRINGS.ssoButton}</Text>
              )}
            </TouchableOpacity>

            {!canSignIn && !isSigningIn ? (
              <Text style={styles.hint}>{APP_STRINGS.ssoPreparing}</Text>
            ) : null}
          </>
        ) : (
          <Text style={styles.error}>{APP_STRINGS.ssoNotConfigured}</Text>
        )}

        {error ? <Text style={styles.error}>{error}</Text> : null}

        <Text style={styles.footnote}>{APP_STRINGS.ssoFootnote}</Text>
      </View>
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: C.canvas,
    alignItems: 'center',
    justifyContent: 'center',
    padding: Spacing.four,
  },
  card: {
    width: '100%',
    maxWidth: MaxContentWidth / 2,
    backgroundColor: C.background,
    borderRadius: Radius.control,
    borderTopWidth: 4,
    borderTopColor: C.washuRed,
    padding: Spacing.four,
    gap: Spacing.three,
  },
  backLink: {
    fontSize: 14,
    fontWeight: '600',
    color: C.textSecondary,
  },
  title: {
    fontSize: 28,
    fontWeight: 'bold',
    color: C.washuRed,
  },
  subtitle: {
    fontSize: 14,
    color: C.textSecondary,
    lineHeight: 20,
  },
  error: {
    color: C.washuRed,
    fontSize: 14,
    lineHeight: 20,
  },
  hint: {
    color: C.textSecondary,
    fontSize: 13,
  },
  button: {
    backgroundColor: C.washuRed,
    borderRadius: 4,
    paddingVertical: Spacing.three,
    alignItems: 'center',
  },
  buttonDisabled: {
    opacity: 0.5,
  },
  buttonText: {
    color: C.washuWhite,
    fontSize: 16,
    fontWeight: '700',
  },
  footnote: {
    fontSize: 12,
    color: C.textPlaceholder,
    lineHeight: 17,
  },
});

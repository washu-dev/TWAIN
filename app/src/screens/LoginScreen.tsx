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
import { APP_STRINGS, MaxContentWidth, Spacing } from '@/constants/theme';
import { useAuth } from '@/hooks/useAuth';

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
                <ActivityIndicator color="#FFFFFF" />
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
    backgroundColor: '#F5F5F5',
    alignItems: 'center',
    justifyContent: 'center',
    padding: Spacing.four,
  },
  card: {
    width: '100%',
    maxWidth: MaxContentWidth / 2,
    backgroundColor: '#FFFFFF',
    borderRadius: 8,
    borderTopWidth: 4,
    borderTopColor: '#BA0C2F',
    padding: Spacing.four,
    gap: Spacing.three,
  },
  backLink: {
    fontSize: 14,
    fontWeight: '600',
    color: '#5A5A5A',
  },
  title: {
    fontSize: 28,
    fontWeight: 'bold',
    color: '#BA0C2F',
  },
  subtitle: {
    fontSize: 14,
    color: '#5A5A5A',
    lineHeight: 20,
  },
  error: {
    color: '#BA0C2F',
    fontSize: 14,
    lineHeight: 20,
  },
  hint: {
    color: '#5A5A5A',
    fontSize: 13,
  },
  button: {
    backgroundColor: '#BA0C2F',
    borderRadius: 4,
    paddingVertical: Spacing.three,
    alignItems: 'center',
  },
  buttonDisabled: {
    opacity: 0.5,
  },
  buttonText: {
    color: '#FFFFFF',
    fontSize: 16,
    fontWeight: '700',
  },
  footnote: {
    fontSize: 12,
    color: '#999999',
    lineHeight: 17,
  },
});

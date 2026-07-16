import React, { useState } from 'react';
import {
  View,
  Text,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { APP_STRINGS, MaxContentWidth, Spacing } from '@/constants/theme';
import { useAuth } from '@/hooks/useAuth';

export const LoginScreen: React.FC = () => {
  const { signIn } = useAuth();
  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async () => {
    const value = email.trim();
    if (!value || loading) return;
    setError(null);
    setLoading(true);
    try {
      await signIn(value);
      // On success the auth guard swaps the navigator to the app routes.
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail;
      setError(typeof detail === 'string' ? detail : APP_STRINGS.loginErrorGeneric);
    } finally {
      setLoading(false);
    }
  };

  return (
    <SafeAreaView style={styles.container}>
      <View style={styles.card}>
        <Text style={styles.title}>{APP_STRINGS.appTitle}</Text>
        <Text style={styles.subtitle}>{APP_STRINGS.loginPrompt}</Text>

        <TextInput
          style={styles.input}
          value={email}
          onChangeText={setEmail}
          placeholder={APP_STRINGS.emailPlaceholder}
          placeholderTextColor="#999999"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="email-address"
          inputMode="email"
          onSubmitEditing={handleSubmit}
          accessibilityLabel={APP_STRINGS.emailPlaceholder}
        />

        {error ? <Text style={styles.error}>{error}</Text> : null}

        <TouchableOpacity
          style={[styles.button, (!email.trim() || loading) && styles.buttonDisabled]}
          onPress={handleSubmit}
          disabled={!email.trim() || loading}
          accessibilityRole="button"
          accessibilityLabel={APP_STRINGS.signInButton}
        >
          {loading ? (
            <ActivityIndicator color="#FFFFFF" />
          ) : (
            <Text style={styles.buttonText}>{APP_STRINGS.signInButton}</Text>
          )}
        </TouchableOpacity>
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
  input: {
    borderWidth: 1,
    borderColor: '#CCCCCC',
    borderRadius: 4,
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    fontSize: 16,
    color: '#000000',
    backgroundColor: '#FFFFFF',
  },
  error: {
    color: '#BA0C2F',
    fontSize: 14,
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
});

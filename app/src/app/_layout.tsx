import { Stack } from 'expo-router';
import { ActivityIndicator, View } from 'react-native';
import { useColorScheme } from '@/hooks/useColorScheme';
import { Colors } from '@/constants/theme';
import { AuthProvider, useAuth } from '@/hooks/useAuth';

function RootNavigator() {
  const colorScheme = useColorScheme();
  const colors = Colors[colorScheme === 'dark' ? 'dark' : 'light'];
  const { isAuthenticated, isLoading } = useAuth();

  // Hold on a splash while we validate any stored session, so an already
  // signed-in user isn't flashed the landing/login screen on load.
  if (isLoading) {
    return (
      <View
        style={{
          flex: 1,
          alignItems: 'center',
          justifyContent: 'center',
          backgroundColor: colors.background,
        }}
      >
        <ActivityIndicator size="large" color={colors.washuRed} accessibilityLabel="Loading" />
      </View>
    );
  }

  return (
    <Stack
      screenOptions={{
        headerShown: false,
        contentStyle: {
          backgroundColor: colors.background,
        },
      }}
    >
      {/* Always-present anchor. Expo Router redirects to it whenever a guarded
          screen is removed, so it is where both auth states land: index renders
          the public landing page when signed out and redirects to /dashboard
          when signed in (see app/index.tsx). */}
      <Stack.Screen name="index" options={{ title: 'TWAIN' }} />

      {/* Authenticated app routes. Removed from the navigator when the guard is
          false, so none of the app is reachable until the user signs in. */}
      <Stack.Protected guard={isAuthenticated}>
        <Stack.Screen name="dashboard" options={{ title: 'TWAIN' }} />
        <Stack.Screen name="chat" options={{ title: 'TWAIN Chat' }} />
        <Stack.Screen name="browse" options={{ title: 'Simulations' }} />
        <Stack.Screen name="report" options={{ title: 'Report' }} />
        <Stack.Screen name="settings" options={{ title: 'Settings' }} />
        {/* The notification emails' deep link (/conversations/<id>). Expo Router
            registers every file route whether or not it is named here, so leaving
            it out did not make it public-by-omission -- it made it reachable
            while signed out, where ChatScreen fetched immediately and the API
            answered 401 instead of the visitor being asked to sign in. */}
        <Stack.Screen name="conversations/[id]" options={{ title: 'TWAIN Chat' }} />
      </Stack.Protected>

      {/* Login is only reachable while signed out. */}
      <Stack.Protected guard={!isAuthenticated}>
        <Stack.Screen name="login" options={{ title: 'Sign in' }} />
      </Stack.Protected>
    </Stack>
  );
}

export default function RootLayout() {
  return (
    <AuthProvider>
      <RootNavigator />
    </AuthProvider>
  );
}

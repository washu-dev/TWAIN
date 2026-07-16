import { Stack } from 'expo-router';
import { useColorScheme } from '@/hooks/useColorScheme';
import { Colors } from '@/constants/theme';
import { AuthProvider, useAuth } from '@/hooks/useAuth';

function RootNavigator() {
  const colorScheme = useColorScheme();
  const colors = Colors[colorScheme === 'dark' ? 'dark' : 'light'];
  const { isAuthenticated } = useAuth();

  return (
    <Stack
      screenOptions={{
        headerShown: false,
        contentStyle: {
          backgroundColor: colors.background,
        },
      }}
    >
      {/* Authenticated app routes. Expo Router removes these from the navigator
          when the guard is false and redirects to the only remaining screen. */}
      <Stack.Protected guard={isAuthenticated}>
        <Stack.Screen name="index" options={{ title: 'TWAIN' }} />
        <Stack.Screen name="chat" options={{ title: 'TWAIN Chat' }} />
        <Stack.Screen name="browse" options={{ title: 'Simulations' }} />
        <Stack.Screen name="report" options={{ title: 'Report' }} />
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

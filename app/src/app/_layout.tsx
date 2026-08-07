import { Stack } from 'expo-router';
import { ActivityIndicator, View } from 'react-native';
import { useColorScheme } from '@/hooks/useColorScheme';
import { Colors, Motion } from '@/constants/theme';
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
        // Screens slide in from the right and the outgoing one eases out under
        // them, which is the platform-native reading of "deeper into the app".
        // `simple_push` rather than the default because it does not dim or scale
        // the outgoing screen -- at this app's page weights that read as a flash.
        animation: 'simple_push',
        animationDuration: Motion.page,
        // Back-swipe from the left edge. Free on native, and it is what makes an
        // app feel like an app rather than a site in a shell.
        gestureEnabled: true,
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
        <Stack.Screen name="libraries" options={{ title: 'What TWAIN can run' }} />
        <Stack.Screen name="report" options={{ title: 'Report' }} />
        <Stack.Screen name="settings" options={{ title: 'Settings' }} />
        {/* Same reason as the deep link below: signing out is what REMOVES these
            screens -- signOut only clears session state, it never navigates. Left
            outside the guard, the tutorial stayed mounted after sign-out, still
            offering a "Sign out" button that did nothing and a "Start a
            simulation" button pointing at a route no longer in the navigator. */}
        <Stack.Screen name="tutorial" options={{ title: 'How TWAIN works' }} />
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

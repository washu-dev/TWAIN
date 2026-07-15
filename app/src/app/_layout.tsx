import { Stack } from 'expo-router';
import { useColorScheme } from '@/hooks/useColorScheme';
import { Colors } from '@/constants/theme';

export default function RootLayout() {
  const colorScheme = useColorScheme();
  const colors = Colors[colorScheme === 'dark' ? 'dark' : 'light'];

  return (
    <Stack
      screenOptions={{
        headerShown: false,
        contentStyle: {
          backgroundColor: colors.background,
        },
      }}
    >
      <Stack.Screen name="index" options={{ title: 'TWAIN' }} />
      <Stack.Screen name="chat" options={{ title: 'TWAIN Chat' }} />
      <Stack.Screen name="browse" options={{ title: 'Simulations' }} />
      <Stack.Screen name="report" options={{ title: 'Report' }} />
    </Stack>
  );
}

import { Redirect } from 'expo-router';
import { LandingScreen } from '@/screens/LandingScreen';
import { useAuth } from '@/hooks/useAuth';

// The always-present anchor route (`/`). Expo Router redirects here whenever a
// guarded screen is removed (see _layout.tsx), so it decides where each auth
// state lands: signed-in users go straight to the app dashboard; signed-out
// visitors see the public landing page, whose only way forward is to sign in.
export default function Index() {
  const { isAuthenticated } = useAuth();

  if (isAuthenticated) {
    return <Redirect href="/dashboard" />;
  }

  return <LandingScreen />;
}

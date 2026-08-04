import { Redirect } from 'expo-router';
import { LandingScreen } from '@/screens/LandingScreen';
import { useAuth } from '@/hooks/useAuth';
import { takeIntendedPath } from '@/utils/deepLink';

// The always-present anchor route (`/`). Expo Router redirects here whenever a
// guarded screen is removed (see _layout.tsx), so it decides where each auth
// state lands: signed-in users go straight to the app dashboard; signed-out
// visitors see the public landing page, whose only way forward is to sign in.
export default function Index() {
  const { isAuthenticated } = useAuth();

  if (isAuthenticated) {
    // A visitor bounced off a guarded route (a notification email's link to
    // /conversations/<id>) lands here after signing in. Send them where they
    // were going; the dashboard is only the fallback.
    const intended = takeIntendedPath();
    return <Redirect href={(intended ?? '/dashboard') as never} />;
  }

  return <LandingScreen />;
}

import { ChatScreen } from '@/screens/ChatScreen';

// Deep-link entry point: `/conversations/:id` (web) and `twain://conversations/:id`
// (native) both resolve here via expo-router's file-based routing. This is the
// exact path the runner's suspend notifications link back to (see
// runner/notifications.py `_resume_hint`), so tapping an email/SMS "return to your
// run" link lands the researcher directly in the conversation. ChatScreen reads
// the `id` segment from useLocalSearchParams, the same param the /chat?id= route
// provides, so the screen is identical for both.
export default function ConversationDeepLink() {
  return <ChatScreen />;
}

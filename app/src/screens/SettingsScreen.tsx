import React, { useEffect, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  Switch,
  TouchableOpacity,
  ActivityIndicator,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { Header, Footer } from '@/components';
import { APP_STRINGS, Colors, Spacing } from '@/constants/theme';
import { apiClient, NOTIFY_KINDS, NotifyKind } from '@/api/client';
import { useAuth } from '@/hooks/useAuth';

const C = Colors.light;

// What each runner notification kind means, in the user's words.
const KIND_LABELS: Record<NotifyKind, { title: string; description: string }> = {
  input: {
    title: 'Needs your input',
    description: 'A run paused on a clarification question or confirmation.',
  },
  approval: {
    title: 'Plan ready for approval',
    description: 'A plan is waiting for you to review and approve.',
  },
  completed: {
    title: 'Run completed',
    description: 'A run finished successfully.',
  },
  failed: {
    title: 'Run failed',
    description: 'A run ended in an error.',
  },
  terminated: {
    title: 'Run terminated',
    description: 'A run you asked to stop has been fully shut down.',
  },
};

export const SettingsScreen: React.FC = () => {
  const router = useRouter();
  const { signOut } = useAuth();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  // The API health check, rehomed from the dashboard header. Its result is one
  // line, so it reports in place rather than in a modal -- a modal was the only
  // reason the dashboard carried MessageModal at all.
  const [apiStatus, setApiStatus] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [kinds, setKinds] = useState<Record<string, boolean>>(
    Object.fromEntries(NOTIFY_KINDS.map((k) => [k, true])),
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const user = await apiClient.me();
        if (cancelled) return;
        const prefs = user.notify_prefs ?? {};
        setEnabled(prefs.enabled !== false);
        // Missing kind = "send" (matches the runner's interpretation).
        setKinds(
          Object.fromEntries(
            NOTIFY_KINDS.map((k) => [k, prefs.kinds?.[k] !== false]),
          ),
        );
      } catch {
        if (!cancelled) setStatus('Could not load your settings. Try reloading.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const save = async () => {
    setSaving(true);
    setStatus(null);
    try {
      await apiClient.updateNotifyPrefs({ enabled, kinds });
      setStatus('Saved.');
    } catch {
      setStatus('Saving failed. Check your connection and try again.');
    } finally {
      setSaving(false);
    }
  };

  const checkApi = async () => {
    setChecking(true);
    setApiStatus(null);
    try {
      const { status } = await apiClient.health();
      setApiStatus(`Reachable — status: ${status}`);
    } catch (error) {
      // Named subject first: the raw axios message is "Network Error", which on
      // its own does not say WHAT could not be reached -- and this row exists to
      // be read at the moment nothing works.
      const detail = error instanceof Error ? error.message : String(error);
      setApiStatus(`Could not reach the API — ${detail}`);
    } finally {
      setChecking(false);
    }
  };

  return (
    <SafeAreaView style={styles.container} edges={['left', 'right', 'bottom']}>
      <Header onLoginPress={signOut} loginLabel={APP_STRINGS.signOutButton} />

      <ScrollView
        style={styles.scroll}
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={Platform.OS !== 'web'}
      >
        {/* Direct loads (URL / refresh) have no history; fall back to home. */}
        <TouchableOpacity
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))}
          accessibilityRole="button"
          style={styles.backButton}
        >
          <Text style={styles.backText}>‹ Back to dashboard</Text>
        </TouchableOpacity>

        <Text style={styles.pageTitle}>Settings</Text>

        <View style={styles.card}>
          <Text style={styles.sectionTitle}>Email notifications</Text>
          <Text style={styles.sectionDesc}>
            TWAIN emails you when a run needs attention or finishes. Turn emails
            off entirely, or choose which events you want to hear about.
          </Text>

          <View style={styles.row}>
            <View style={styles.rowText}>
              <Text style={styles.rowTitle}>Send me emails</Text>
              <Text style={styles.rowDesc}>Master switch for all notifications.</Text>
            </View>
            <Switch
              value={enabled}
              onValueChange={setEnabled}
              trackColor={{ true: C.washuRed }}
              accessibilityLabel="Send me emails"
            />
          </View>

          <View style={styles.divider} />

          {NOTIFY_KINDS.map((kind) => (
            <View key={kind} style={[styles.row, !enabled && styles.rowDisabled]}>
              <View style={styles.rowText}>
                <Text style={styles.rowTitle}>{KIND_LABELS[kind].title}</Text>
                <Text style={styles.rowDesc}>{KIND_LABELS[kind].description}</Text>
              </View>
              <Switch
                value={enabled && kinds[kind]}
                disabled={!enabled}
                onValueChange={(value) =>
                  setKinds((prev) => ({ ...prev, [kind]: value }))
                }
                trackColor={{ true: C.washuRed }}
                accessibilityLabel={KIND_LABELS[kind].title}
              />
            </View>
          ))}

          <TouchableOpacity
            style={[styles.saveButton, saving && styles.saveButtonBusy]}
            onPress={save}
            disabled={saving || loading}
            accessibilityRole="button"
            accessibilityLabel="Save settings"
          >
            <Text style={styles.saveButtonText}>{saving ? 'Saving…' : 'Save'}</Text>
          </TouchableOpacity>

          {status && <Text style={styles.status}>{status}</Text>}
        </View>

        <View style={styles.card}>
          <Text style={styles.sectionTitle}>Diagnostics</Text>
          <Text style={styles.sectionDesc}>
            If the app seems stuck or a screen will not load, check that it can
            still reach the TWAIN backend.
          </Text>
          <View style={styles.row}>
            <View style={styles.rowText}>
              <Text style={styles.rowTitle}>{APP_STRINGS.apiCheck}</Text>
              <Text style={styles.rowDesc}>{APP_STRINGS.apiCheckDesc}</Text>
            </View>
            <TouchableOpacity
              style={[styles.checkButton, checking && styles.saveButtonBusy]}
              onPress={checkApi}
              disabled={checking}
              accessibilityRole="button"
              accessibilityLabel={APP_STRINGS.apiCheck}
            >
              <Text style={styles.checkButtonText}>{checking ? 'Checking…' : 'Check'}</Text>
            </TouchableOpacity>
          </View>
          {apiStatus && <Text style={styles.status}>{apiStatus}</Text>}
        </View>

        {loading && (
          <View style={styles.loading}>
            <ActivityIndicator size="large" color={C.washuRed} accessibilityLabel="Loading" />
          </View>
        )}
      </ScrollView>

      <Footer />
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: C.washuLightGray,
  },
  scroll: {
    flex: 1,
  },
  content: {
    flexGrow: 1,
    paddingHorizontal: Spacing.four,
    paddingTop: Spacing.four,
    paddingBottom: Spacing.four,
    maxWidth: 720,
    width: '100%',
    alignSelf: 'center',
  },
  checkButton: {
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: C.washuRed,
  },
  checkButtonText: {
    color: C.washuRed,
    fontSize: 14,
    fontWeight: '600',
  },
  backButton: {
    alignSelf: 'flex-start',
    marginBottom: Spacing.two,
  },
  backText: {
    fontSize: 15,
    fontWeight: '600',
    color: C.washuRed,
  },
  pageTitle: {
    fontSize: 24,
    fontWeight: '700',
    color: C.textStrong,
    marginBottom: Spacing.three,
  },
  card: {
    backgroundColor: C.background,
    borderRadius: 8,
    padding: Spacing.four,
    borderWidth: 1,
    borderColor: C.border,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    color: C.textStrong,
  },
  sectionDesc: {
    fontSize: 14,
    color: C.textSecondary,
    marginTop: Spacing.one,
    marginBottom: Spacing.three,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: Spacing.two,
  },
  rowDisabled: {
    opacity: 0.45,
  },
  rowText: {
    flex: 1,
    paddingRight: Spacing.three,
  },
  rowTitle: {
    fontSize: 15,
    fontWeight: '600',
    color: C.textStrong,
  },
  rowDesc: {
    fontSize: 13,
    color: C.textSecondary,
    marginTop: 2,
  },
  divider: {
    height: 1,
    backgroundColor: C.divider,
    marginVertical: Spacing.two,
  },
  saveButton: {
    marginTop: Spacing.three,
    backgroundColor: C.washuRed,
    borderRadius: 6,
    paddingVertical: Spacing.two,
    alignItems: 'center',
  },
  saveButtonBusy: {
    opacity: 0.6,
  },
  saveButtonText: {
    color: C.washuWhite,
    fontSize: 16,
    fontWeight: '600',
  },
  status: {
    marginTop: Spacing.two,
    fontSize: 14,
    color: C.washuGreen,
    textAlign: 'center',
  },
  loading: {
    alignItems: 'center',
    paddingVertical: Spacing.four,
  },
});

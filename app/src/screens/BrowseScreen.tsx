import React, { useCallback, useEffect, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { apiClient, Conversation } from '@/api/client';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

const TERMINAL_STATUSES = ['completed', 'error', 'rejected', 'cancelled'];

export const BrowseScreen: React.FC = () => {
  const router = useRouter();
  const [items, setItems] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await apiClient.listConversations());
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load runs');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiClient.listConversations();
        if (!cancelled) setItems(data);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load runs');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <SafeAreaView style={styles.container} edges={['top', 'left', 'right', 'bottom']}>
      <View style={styles.topBar}>
        {/* Direct loads (URL / refresh) have no history; fall back to home. */}
        <TouchableOpacity
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))}
          accessibilityRole="button"
        >
          <Text style={styles.back}>‹ Back</Text>
        </TouchableOpacity>
        <Text style={styles.title}>Simulations</Text>
        <TouchableOpacity onPress={load} accessibilityRole="button">
          <Text style={styles.refresh}>Refresh</Text>
        </TouchableOpacity>
      </View>

      {loading && <ActivityIndicator style={{ marginTop: Spacing.five }} color={C.washuRed} />}
      {error && <Text style={styles.error}>{error}</Text>}

      {!loading && !error && (
        <ScrollView style={styles.scroll} contentContainerStyle={styles.content}>
          {items.length === 0 && (
            <Text style={styles.empty}>No simulations yet. Start one from the home screen.</Text>
          )}
          {items.map((item) => (
            <TouchableOpacity
              key={item.id}
              style={styles.rowItem}
              // Active runs open the chat (where clarifications and the plan
              // approval live); only finished runs go straight to the report.
              onPress={() =>
                router.push({
                  pathname: TERMINAL_STATUSES.includes(item.status) ? '/report' : '/chat',
                  params: { id: item.id },
                })
              }
              accessibilityRole="button"
            >
              <View style={styles.rowMain}>
                <Text style={styles.rowTitle} numberOfLines={1}>
                  {item.title || 'Untitled run'}
                </Text>
                <Text style={styles.rowMeta}>
                  {item.current_state} · {new Date(item.updated_at).toLocaleString()}
                </Text>
              </View>
              <StatusBadge status={item.status} />
            </TouchableOpacity>
          ))}
        </ScrollView>
      )}
    </SafeAreaView>
  );
};

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const color =
    status === 'completed' ? C.washuGreen : status === 'error' ? C.washuRed : C.textSecondary;
  return (
    <View style={[styles.badge, { backgroundColor: color }]}>
      <Text style={styles.badgeText}>{status}</Text>
    </View>
  );
};

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: C.background },
  topBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    backgroundColor: C.washuRed,
  },
  back: { color: '#FFFFFF', fontSize: 16, fontWeight: '600' },
  title: { color: '#FFFFFF', fontSize: 16, fontWeight: '700' },
  refresh: { color: '#FFFFFF', fontSize: 14, fontWeight: '600' },
  scroll: { flex: 1 },
  content: { padding: Spacing.three, gap: Spacing.two },
  empty: { color: C.textSecondary, fontSize: 14, fontStyle: 'italic', padding: Spacing.two },
  rowItem: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    padding: Spacing.three,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: C.backgroundElement,
  },
  rowMain: { flex: 1, gap: 2 },
  rowTitle: { fontSize: 15, fontWeight: '600', color: C.text },
  rowMeta: { fontSize: 12, color: C.textSecondary },
  badge: { borderRadius: 12, paddingHorizontal: Spacing.two, paddingVertical: 3 },
  badgeText: { color: '#FFFFFF', fontWeight: '700', fontSize: 11 },
  error: { color: C.washuRed, padding: Spacing.three },
});

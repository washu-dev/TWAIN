import React, { useEffect, useState } from 'react';
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
import { apiClient, LibraryAvailability, LibraryInfo } from '@/api/client';
import { useAuth } from '@/hooks/useAuth';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

const SECTIONS: { kind: LibraryInfo['kind']; title: string; blurb: string }[] = [
  {
    kind: 'calculator',
    title: 'Calculators',
    blurb: 'Engines that compute a property — DFT codes, semi-empirical methods, ML potentials.',
  },
  {
    kind: 'library',
    title: 'Libraries',
    blurb: 'Toolkits TWAIN builds a run on — structure handling, descriptors, analysis.',
  },
];

function freshness(iso?: string | null): string {
  if (!iso) return 'not yet probed';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return 'not yet probed';
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 60) return `checked ${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `checked ${hours} h ago`;
  return `checked ${Math.floor(hours / 24)} d ago`;
}

export const LibrariesScreen: React.FC = () => {
  const router = useRouter();
  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const [data, setData] = useState<LibraryAvailability | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Same auth gate as the other screens: fetching before a token exists 401s.
    if (authLoading || !isAuthenticated) return;
    let cancelled = false;
    (async () => {
      try {
        const result = await apiClient.listLibraries();
        if (!cancelled) setData(result);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load libraries');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authLoading, isAuthenticated]);

  const rows = data?.libraries ?? [];

  return (
    <SafeAreaView style={styles.container} edges={['top', 'left', 'right', 'bottom']}>
      <View style={styles.topBar}>
        <TouchableOpacity
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/dashboard'))}
          accessibilityRole="button"
        >
          <Text style={styles.back}>‹ Back</Text>
        </TouchableOpacity>
        <Text style={styles.title}>What TWAIN can run</Text>
        <View style={{ width: 48 }} />
      </View>

      {loading && <ActivityIndicator style={{ marginTop: Spacing.five }} color={C.washuRed} />}
      {error && <Text style={styles.error}>{error}</Text>}

      {!loading && !error && (
        <ScrollView style={styles.scroll} contentContainerStyle={styles.content}>
          <Text style={styles.summary}>
            {rows.length === 0
              ? 'The runner has not published a capability list yet. It probes the cluster on start-up, so this fills in once a runner has restarted.'
              : `${data?.installed ?? 0} of ${data?.total ?? rows.length} installed on this cluster · ${freshness(data?.checked_at)}`}
          </Text>
          {/* Said plainly, because "not installed" is not the same as "unsupported":
              TWAIN knows how to plan with everything listed here, and anything
              missing can be provisioned. */}
          {rows.length > 0 && (
            <Text style={styles.note}>
              Everything listed is something TWAIN knows how to plan with. Items marked
              “not installed” need provisioning on the cluster before a run can use them.
            </Text>
          )}

          {SECTIONS.map((section) => {
            const items = rows.filter((row) => row.kind === section.kind);
            if (items.length === 0) return null;
            return (
              <View key={section.kind} style={styles.section}>
                <Text style={styles.sectionTitle}>{section.title}</Text>
                <Text style={styles.sectionBlurb}>{section.blurb}</Text>
                {items.map((item) => (
                  <View
                    key={`${item.kind}:${item.name}`}
                    style={[
                      styles.row,
                      { borderLeftColor: item.installed ? C.washuGreen : C.textSecondary },
                    ]}
                  >
                    <View style={styles.rowHead}>
                      <Text style={styles.rowName}>{item.name}</Text>
                      <Text
                        style={[
                          styles.badge,
                          { color: item.installed ? C.washuGreen : C.textSecondary },
                        ]}
                      >
                        {item.installed ? 'installed' : 'not installed'}
                      </Text>
                    </View>
                    {!!item.version && <Text style={styles.rowMeta}>{`v${item.version}`}</Text>}
                    {!!item.description && (
                      <Text style={styles.rowDesc} numberOfLines={3}>
                        {item.description}
                      </Text>
                    )}
                    {!!item.detail && <Text style={styles.rowDetail}>{item.detail}</Text>}
                  </View>
                ))}
              </View>
            );
          })}
        </ScrollView>
      )}
    </SafeAreaView>
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
  back: { color: '#FFFFFF', fontSize: 16, fontWeight: '600', width: 48 },
  title: { color: '#FFFFFF', fontSize: 16, fontWeight: '700', flex: 1, textAlign: 'center' },
  scroll: { flex: 1 },
  content: { padding: Spacing.three, gap: Spacing.two },
  summary: { color: C.text, fontSize: 15, fontWeight: '600' },
  note: { color: C.textSecondary, fontSize: 13, lineHeight: 18 },
  error: { color: C.washuRed, padding: Spacing.three },
  section: { marginTop: Spacing.three, gap: Spacing.one },
  sectionTitle: { color: C.text, fontSize: 17, fontWeight: '700' },
  sectionBlurb: { color: C.textSecondary, fontSize: 13, marginBottom: Spacing.one },
  row: {
    borderLeftWidth: 3,
    backgroundColor: C.backgroundElement,
    borderRadius: 6,
    padding: Spacing.two,
    marginBottom: Spacing.one,
    gap: 2,
  },
  rowHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  rowName: { color: C.text, fontSize: 15, fontWeight: '600', flexShrink: 1 },
  badge: { fontSize: 12, fontWeight: '700' },
  rowMeta: { color: C.textSecondary, fontSize: 12 },
  rowDesc: { color: C.text, fontSize: 13, lineHeight: 18 },
  rowDetail: { color: C.textSecondary, fontSize: 12, fontStyle: 'italic' },
});

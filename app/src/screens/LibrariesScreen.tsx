import React, { useEffect, useMemo, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  TouchableOpacity,
  TextInput,
  StyleSheet,
  ActivityIndicator,
  Linking,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { apiClient, LibraryAvailability, LibraryInfo } from '@/api/client';
import { useAuth } from '@/hooks/useAuth';
import { APP_STRINGS, Colors, Spacing } from '@/constants/theme';

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

/**
 * Everything about a row worth matching a query against.
 *
 * Deliberately more than the name: `detail` carries the environment that provides
 * a package ("importable as 'rdkit' in the default env"), so searching "gpaw"
 * finds what the gpaw env supplies, and searching "not found" finds what needs
 * provisioning. Searching only names would make the field decoration.
 *
 * `installed` is NOT folded in as a word -- "not installed" contains "installed",
 * so the obvious query would match every row and quietly mean nothing.
 */
function haystack(row: LibraryInfo): string {
  return [row.name, row.import_name, row.version, row.description, row.detail, row.env]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

/**
 * A short label for a URL: the part a reader recognises.
 *
 * The host alone, except on a code forge, where the host IS the same for every
 * project and says nothing -- two entries both reading "github.com" identify
 * neither, so those keep owner/repo. Deep documentation paths are still dropped:
 * "docs.ase-lib.org" is the useful half of
 * "docs.ase-lib.org/ase/calculators/emt.html".
 */
const FORGES = ['github.com', 'gitlab.com', 'bitbucket.org', 'codeberg.org'];

function linkLabel(url: string): string {
  const match = url.match(/^https?:\/\/([^/]+)(\/[^?#]*)?/i);
  if (!match) return url;
  const host = match[1].replace(/^www\./, '');
  if (!FORGES.includes(host)) return host;
  const segments = (match[2] ?? '').split('/').filter(Boolean).slice(0, 2);
  return segments.length ? `${host}/${segments.join('/')}` : host;
}

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
  const [query, setQuery] = useState('');

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
  const trimmed = query.trim().toLowerCase();
  // Every whitespace-separated term must match, so "gpaw not" narrows rather than
  // widening the way a single-substring match would.
  const terms = trimmed ? trimmed.split(/\s+/) : [];
  const matches = useMemo(
    () => (terms.length === 0 ? rows : rows.filter((row) => {
      const text = haystack(row);
      return terms.every((term) => text.includes(term));
    })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rows, trimmed],
  );

  return (
    <SafeAreaView style={styles.container} edges={['top', 'left', 'right', 'bottom']}>
      <View style={styles.topBar}>
        <TouchableOpacity
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/dashboard'))}
          accessibilityRole="button"
        >
          <Text style={styles.back}>‹ Back</Text>
        </TouchableOpacity>
        <Text style={styles.title}>{APP_STRINGS.librariesScreenTitle}</Text>
        <View style={{ width: 48 }} />
      </View>

      {/* Outside the ScrollView on purpose: a filter you have to scroll back up to
          reach stops being used. Only shown once there is something to filter. */}
      {!loading && !error && rows.length > 0 && (
        <View style={styles.searchRow}>
          <Text style={styles.searchIcon} aria-hidden>
            ⌕
          </Text>
          <TextInput
            style={styles.searchInput}
            value={query}
            onChangeText={setQuery}
            placeholder="Search name, description or environment"
            placeholderTextColor={C.textPlaceholder}
            autoCapitalize="none"
            autoCorrect={false}
            returnKeyType="search"
            accessibilityLabel="Search libraries and engines"
          />
          {query.length > 0 && (
            <TouchableOpacity
              onPress={() => setQuery('')}
              accessibilityRole="button"
              accessibilityLabel="Clear search"
              style={styles.clearButton}
            >
              <Text style={styles.clearIcon}>×</Text>
            </TouchableOpacity>
          )}
        </View>
      )}

      {loading && <ActivityIndicator style={{ marginTop: Spacing.five }} color={C.washuRed} />}
      {error && <Text style={styles.error}>{error}</Text>}

      {!loading && !error && (
        <ScrollView style={styles.scroll} contentContainerStyle={styles.content}>
          <Text style={styles.summary}>
            {rows.length === 0
              ? 'The runner has not published a capability list yet. It probes the cluster on start-up, so this fills in once a runner has restarted.'
              : terms.length > 0
                ? `${matches.length} of ${rows.length} match “${query.trim()}”${
                    matches.length > 0
                      ? ` · ${matches.filter((m) => m.installed).length} installed`
                      : ''
                  }`
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

          {terms.length > 0 && matches.length === 0 && (
            <Text style={styles.note}>
              Nothing matches that. Searching covers names, descriptions, import
              names and the environment a package comes from — try a shorter term.
            </Text>
          )}

          {SECTIONS.map((section) => {
            const items = matches.filter((row) => row.kind === section.kind);
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
                    {!!item.homepage && (
                      <TouchableOpacity
                        onPress={() => Linking.openURL(item.homepage as string)}
                        accessibilityRole="link"
                        accessibilityLabel={`Open the ${item.name} website, ${linkLabel(
                          item.homepage,
                        )}`}
                        style={styles.linkRow}
                      >
                        <Text style={styles.link}>{linkLabel(item.homepage)}</Text>
                        <Text style={styles.linkArrow} aria-hidden>
                          ↗
                        </Text>
                      </TouchableOpacity>
                    )}
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
  back: { color: C.washuWhite, fontSize: 16, fontWeight: '600', width: 48 },
  title: { color: C.washuWhite, fontSize: 16, fontWeight: '700', flex: 1, textAlign: 'center' },
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
  searchRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    marginHorizontal: Spacing.three,
    marginTop: Spacing.three,
    paddingHorizontal: Spacing.two,
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 8,
    backgroundColor: C.background,
  },
  searchIcon: { fontSize: 16, color: C.textSecondary },
  searchInput: {
    flex: 1,
    paddingVertical: Spacing.two,
    fontSize: 15,
    color: C.text,
    // Suppresses the browser's own focus ring, which sat outside our border.
    // outlineWidth rather than outlineStyle: RN 0.85 types the latter as
    // solid/dotted/dashed only, so 'none' does not typecheck.
    outlineWidth: 0,
  },
  clearButton: { paddingHorizontal: Spacing.one, paddingVertical: Spacing.half },
  clearIcon: { fontSize: 20, color: C.textSecondary, lineHeight: 22 },
  // Its own row so the tap target is the link, not the whole card.
  linkRow: { flexDirection: 'row', alignItems: 'center', gap: 4, paddingTop: 2 },
  link: { color: C.info, fontSize: 12, fontWeight: '600' },
  linkArrow: { color: C.info, fontSize: 11 },
});

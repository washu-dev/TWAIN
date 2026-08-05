import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  TextInput,
  TouchableOpacity,
  Modal,
  StyleSheet,
  ActivityIndicator,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useFocusEffect, useRouter } from 'expo-router';
import { apiClient, Conversation, ConversationStatus } from '@/api/client';
import { useNow } from '@/hooks/useNow';
import { formatElapsed } from '@/utils/duration';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

// How often to re-read the list while any run is still going. The list is one
// small query and only polls while something can actually change.
const LIVE_POLL_MS = 5000;

const TERMINAL_STATUSES = ['completed', 'error', 'rejected', 'cancelled'];

type Group = 'attention' | 'running' | 'done';

// Friendly, jargon-free status per conversation status, plus which section it
// belongs to and its accent color.
// Typed as Record<ConversationStatus, ...> on purpose: a status the backend can
// write but this map has no entry for used to fall through to DEFAULT_META and
// render as "Unknown". That is what a terminated run showed -- the runner writes
// 'cancelled' (runner.py _finalize_cancelled) and only this map was missing it.
// Keying on the union makes the next such omission a compile error instead of a
// word the researcher has to interpret.
const STATUS_META: Record<ConversationStatus, { label: string; group: Group; color: string }> = {
  awaiting_approval: { label: 'Needs approval', group: 'attention', color: '#B8860B' },
  awaiting_input: { label: 'Needs your reply', group: 'attention', color: '#B8860B' },
  running: { label: 'Running', group: 'running', color: '#0B69C7' },
  cancelling: { label: 'Stopping…', group: 'running', color: C.textSecondary },
  completed: { label: 'Completed', group: 'done', color: C.washuGreen },
  error: { label: 'Failed', group: 'done', color: C.washuRed },
  rejected: { label: 'Rejected', group: 'done', color: C.textSecondary },
  cancelled: { label: 'Terminated', group: 'done', color: C.textSecondary },
};
// Still needed: `status` arrives as JSON, so a value outside the union is
// possible at runtime even though the map is exhaustive at compile time.
const DEFAULT_META = { label: 'Unknown', group: 'done' as Group, color: C.textSecondary };

// Raw state-machine states → plain phase names (only shown for running runs).
const PHASE: Record<string, string> = {
  INTAKE: 'Understanding request',
  CLARIFY: 'Clarifying',
  DECOMPOSE: 'Breaking into goals',
  DISCOVER: 'Choosing tools',
  PLAN: 'Planning',
  BUILD: 'Building code',
  EXECUTE: 'Running calculation',
  INTERPRET: 'Interpreting results',
  VALIDATE: 'Validating',
  ACCEPT: 'Finalizing',
  TERMINATE: 'Done',
};

const SECTIONS: { group: Group; title: string }[] = [
  { group: 'attention', title: 'Needs your input' },
  { group: 'running', title: 'In progress' },
  { group: 'done', title: 'Finished' },
];

const FILTERS: { key: 'all' | 'active' | 'done'; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'active', label: 'Active' },
  { key: 'done', label: 'Finished' },
];

function metaFor(status: string) {
  // `status` is JSON off the wire, so it is a plain string here even though
  // STATUS_META is keyed on the union. Check membership rather than casting the
  // map to Record<string, ...>, which would give up the exhaustiveness that
  // makes a missing label a compile error.
  return Object.prototype.hasOwnProperty.call(STATUS_META, status)
    ? STATUS_META[status as ConversationStatus]
    : DEFAULT_META;
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d} d ago`;
  return new Date(iso).toLocaleDateString();
}

export const BrowseScreen: React.FC = () => {
  const router = useRouter();
  const [items, setItems] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'active' | 'done'>('all');
  const [query, setQuery] = useState('');
  const [pendingDelete, setPendingDelete] = useState<Conversation | null>(null);
  const [deleting, setDeleting] = useState(false);

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

  // Initial load. Sets state only after the await so we don't setState
  // synchronously inside the effect body (load() is for the Refresh button).
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

  // Re-read without touching `loading`, so a background sweep never flashes the
  // spinner over a list the researcher is reading. Failures are swallowed: the
  // next tick retries, and a transient blip must not replace a good list with an
  // error banner.
  const refresh = useCallback(async () => {
    try {
      setItems(await apiClient.listConversations());
    } catch {
      /* transient -- the next tick or a focus change retries */
    }
  }, []);

  // A status that changes on the server has to change here. Previously the only
  // way to see a run finish, fail, or be terminated was to press Refresh, so the
  // list confidently showed "Running" for something that had ended minutes ago.
  // Polling is gated on there being something live, so a screen full of finished
  // runs issues no requests at all.
  const anyLive = useMemo(
    () => items.some((item) => metaFor(item.status).group !== 'done'),
    [items],
  );
  // ONE clock for the whole list, passed down to the rows. A useNow() per row
  // would mean ten intervals for ten running runs, all doing the same thing.
  const now = useNow(anyLive);
  useEffect(() => {
    if (!anyLive) return;
    const timer = setInterval(() => {
      void refresh();
    }, LIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [anyLive, refresh]);

  // Coming back to this tab re-reads once, which covers the case the poll cannot:
  // everything was finished when the list was last drawn, so nothing was polling,
  // and a run was started or terminated from another screen meanwhile.
  useFocusEffect(
    useCallback(() => {
      void refresh();
    }, [refresh]),
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter((item) => {
      if (filter !== 'all') {
        const group = metaFor(item.status).group;
        if (filter === 'active' ? group === 'done' : group !== 'done') return false;
      }
      if (q && !(item.title ?? '').toLowerCase().includes(q)) return false;
      return true;
    });
  }, [items, filter, query]);

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setError(null);
    try {
      await apiClient.deleteConversation(pendingDelete.id);
      setItems((prev) => prev.filter((i) => i.id !== pendingDelete.id));
      setPendingDelete(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete run');
    } finally {
      setDeleting(false);
    }
  };

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

      {items.length > 0 && (
        <View style={styles.controls}>
          <TextInput
            style={styles.search}
            value={query}
            onChangeText={setQuery}
            placeholder="Search runs…"
            placeholderTextColor={C.textSecondary}
            autoCapitalize="none"
            autoCorrect={false}
            accessibilityLabel="Search runs"
          />
          <View style={styles.filterRow}>
            {FILTERS.map((f) => {
              const active = filter === f.key;
              return (
                <TouchableOpacity
                  key={f.key}
                  style={[styles.chip, active && styles.chipActive]}
                  onPress={() => setFilter(f.key)}
                  accessibilityRole="button"
                  accessibilityState={{ selected: active }}
                >
                  <Text style={[styles.chipText, active && styles.chipTextActive]}>{f.label}</Text>
                </TouchableOpacity>
              );
            })}
          </View>
        </View>
      )}

      {loading && <ActivityIndicator style={{ marginTop: Spacing.five }} color={C.washuRed} />}
      {error && <Text style={styles.error}>{error}</Text>}

      {!loading && (
        <ScrollView style={styles.scroll} contentContainerStyle={styles.content}>
          {items.length === 0 && !error && (
            <Text style={styles.empty}>No simulations yet. Start one from the home screen.</Text>
          )}
          {items.length > 0 && visible.length === 0 && (
            <Text style={styles.empty}>
              {query.trim()
                ? `No runs match “${query.trim()}”.`
                : `No ${filter === 'active' ? 'active' : 'finished'} runs.`}
            </Text>
          )}

          {SECTIONS.map((section) => {
            const rows = visible.filter((item) => metaFor(item.status).group === section.group);
            if (rows.length === 0) return null;
            return (
              <View key={section.group} style={styles.section}>
                <Text style={styles.sectionHeader}>
                  {section.title} · {rows.length}
                </Text>
                {rows.map((item) => (
                  <RunRow
                    key={item.id}
                    item={item}
                    now={now}
                    onPress={() =>
                      router.push({
                        pathname: TERMINAL_STATUSES.includes(item.status) ? '/report' : '/chat',
                        params: { id: item.id },
                      })
                    }
                    onDelete={() => setPendingDelete(item)}
                  />
                ))}
              </View>
            );
          })}
        </ScrollView>
      )}

      <Modal
        visible={!!pendingDelete}
        transparent
        animationType="fade"
        onRequestClose={() => (deleting ? undefined : setPendingDelete(null))}
      >
        <View style={styles.modalBackdrop}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>Delete this run?</Text>
            <Text style={styles.modalName} numberOfLines={2}>
              {pendingDelete?.title || 'Untitled run'}
            </Text>
            <Text style={styles.modalHint}>
              This permanently removes its messages, results, and artifacts.
            </Text>
            <View style={styles.modalButtons}>
              <TouchableOpacity
                style={styles.modalCancel}
                onPress={() => setPendingDelete(null)}
                disabled={deleting}
                accessibilityRole="button"
              >
                <Text style={styles.modalCancelText}>Cancel</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[styles.modalDelete, deleting && styles.disabled]}
                onPress={confirmDelete}
                disabled={deleting}
                accessibilityRole="button"
              >
                <Text style={styles.modalDeleteText}>{deleting ? 'Deleting…' : 'Delete'}</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
};

const RunRow: React.FC<{
  item: Conversation;
  now: number;
  onPress: () => void;
  onDelete: () => void;
}> = ({ item, now, onPress, onDelete }) => {
  const meta = metaFor(item.status);
  // While a run is going, "just now" is all relativeTime(updated_at) can ever say
  // -- the status writes keep bumping it. How long it has been running is the
  // thing the researcher is actually watching for, so show that instead.
  const running = meta.group === 'running' && !!item.started_at;
  const elapsed = running
    ? formatElapsed((now - new Date(item.started_at as string).getTime()) / 1000)
    : null;
  return (
    <View style={[styles.rowItem, { borderLeftColor: meta.color }]}>
      <TouchableOpacity
        style={styles.rowMain}
        onPress={onPress}
        accessibilityRole="button"
        accessibilityLabel={`${item.title || 'Untitled run'}, ${meta.label}`}
      >
        <Text style={styles.rowTitle} numberOfLines={1}>
          {item.title || 'Untitled run'}
        </Text>
        <Text style={styles.rowMeta} numberOfLines={1}>
          <Text style={{ color: meta.color, fontWeight: '600' }}>{meta.label}</Text>
          {`  ·  ${elapsed ?? relativeTime(item.updated_at)}`}
          {item.status === 'running'
            ? `  ·  ${PHASE[item.current_state] ?? item.current_state}`
            : ''}
        </Text>
      </TouchableOpacity>
      <TouchableOpacity
        style={styles.deleteBtn}
        onPress={onDelete}
        accessibilityRole="button"
        accessibilityLabel="Delete run"
        hitSlop={{ top: 12, bottom: 12, left: 12, right: 12 }}
      >
        <Text style={styles.deleteGlyph}>×</Text>
      </TouchableOpacity>
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
  controls: {
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.two,
    gap: Spacing.two,
    borderBottomWidth: 1,
    borderBottomColor: C.backgroundElement,
  },
  search: {
    height: 40,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: '#DDDDDD',
    paddingHorizontal: Spacing.three,
    fontSize: 15,
    color: C.text,
    backgroundColor: C.washuWhite,
  },
  filterRow: { flexDirection: 'row', gap: Spacing.two },
  chip: {
    paddingHorizontal: Spacing.three,
    paddingVertical: Spacing.one,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: '#DDDDDD',
  },
  chipActive: { backgroundColor: C.washuRed, borderColor: C.washuRed },
  chipText: { fontSize: 13, color: C.textSecondary, fontWeight: '600' },
  chipTextActive: { color: '#FFFFFF' },
  scroll: { flex: 1 },
  content: { padding: Spacing.three, gap: Spacing.three },
  empty: { color: C.textSecondary, fontSize: 14, fontStyle: 'italic', padding: Spacing.two },
  section: { gap: Spacing.two },
  sectionHeader: {
    fontSize: 12,
    fontWeight: '700',
    color: C.textSecondary,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  rowItem: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    paddingVertical: Spacing.three,
    paddingHorizontal: Spacing.three,
    borderRadius: 10,
    backgroundColor: C.backgroundElement,
    borderLeftWidth: 4,
  },
  rowMain: { flex: 1, gap: 3 },
  rowTitle: { fontSize: 15, fontWeight: '600', color: C.text },
  rowMeta: { fontSize: 12, color: C.textSecondary },
  deleteBtn: { paddingHorizontal: Spacing.two, alignItems: 'center', justifyContent: 'center' },
  deleteGlyph: { fontSize: 22, color: C.textSecondary, fontWeight: '400', lineHeight: 24 },
  error: { color: C.washuRed, padding: Spacing.three },
  modalBackdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.45)',
    alignItems: 'center',
    justifyContent: 'center',
    padding: Spacing.four,
  },
  modalCard: {
    width: '100%',
    maxWidth: 360,
    backgroundColor: C.washuWhite,
    borderRadius: 12,
    padding: Spacing.four,
    gap: Spacing.two,
  },
  modalTitle: { fontSize: 17, fontWeight: '700', color: C.text },
  modalName: { fontSize: 14, fontWeight: '600', color: C.text },
  modalHint: { fontSize: 13, color: C.textSecondary, lineHeight: 18 },
  modalButtons: { flexDirection: 'row', justifyContent: 'flex-end', gap: Spacing.two, marginTop: Spacing.two },
  modalCancel: {
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#DDDDDD',
  },
  modalCancelText: { fontSize: 15, fontWeight: '600', color: C.text },
  modalDelete: {
    paddingHorizontal: Spacing.four,
    paddingVertical: Spacing.two,
    borderRadius: 8,
    backgroundColor: C.washuRed,
  },
  modalDeleteText: { fontSize: 15, fontWeight: '700', color: '#FFFFFF' },
  disabled: { opacity: 0.5 },
});

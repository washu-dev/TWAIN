import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import * as Linking from 'expo-linking';
import type { RunFailure } from '@/api/client';
import { Colors, Fonts, Radius, Spacing } from '@/constants/theme';

const C = Colors.light;

// The RIS API Portal has a jobs page but no per-job route.
const RIS_PORTAL_JOBS = 'https://d3n2m687w2hvtj.cloudfront.net/jobs';

interface Props {
  failure: RunFailure;
}

/**
 * Where a run stopped, why in plain words, and what to do next.
 *
 * Replaces the bare "The run failed — see the run log for details." with the
 * failure the runner already classified (a `run.error` event's `failure`):
 * the stage, a one-line headline, the cause, the next step, and the full error
 * behind "Show details". Modelled on RETICLE's ETL "Why it stopped" panel.
 */
export function FailureCard({ failure }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <View style={styles.card} accessibilityRole="alert">
      <Text style={styles.kicker}>
        {`Stopped while ${failure.stage_label.toLowerCase()} · ${failure.stage}`}
      </Text>
      <Text style={styles.headline}>{failure.headline}</Text>
      {failure.cause ? <Text style={styles.cause}>{failure.cause}</Text> : null}

      {failure.next_step ? (
        <View style={styles.next}>
          <Text style={styles.nextLabel}>What to do next</Text>
          <Text style={styles.nextText}>{failure.next_step}</Text>
        </View>
      ) : null}

      <View style={styles.actions}>
        {failure.detail ? (
          <Pressable
            onPress={() => setOpen((o) => !o)}
            accessibilityRole="button"
            accessibilityState={{ expanded: open }}
          >
            <Text style={styles.link}>{open ? 'Hide details' : 'Show details'}</Text>
          </Pressable>
        ) : null}
        {failure.job_id ? (
          <Pressable
            onPress={() => void Linking.openURL(RIS_PORTAL_JOBS)}
            accessibilityRole="link"
            accessibilityLabel={`Open Slurm job ${failure.job_id} in the RIS API Portal`}
          >
            <Text style={styles.link}>{`Slurm job ${failure.job_id} in the RIS API Portal →`}</Text>
          </Pressable>
        ) : null}
      </View>

      {open && failure.detail ? (
        // Selectable so it can be copied into an issue or a message.
        <Text style={styles.detail} selectable>
          {failure.detail}
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    marginHorizontal: Spacing.two,
    marginVertical: Spacing.one,
    padding: Spacing.three,
    gap: Spacing.one,
    borderRadius: Radius.card,
    borderLeftWidth: 4,
    borderLeftColor: C.washuRed,
    backgroundColor: C.backgroundElement,
  },
  kicker: { color: C.washuRed, fontSize: 12, fontWeight: '700', textTransform: 'uppercase' },
  headline: { color: C.textStrong, fontSize: 15, fontWeight: '700' },
  cause: { color: C.textSecondary, fontSize: 13 },
  next: { marginTop: Spacing.one, gap: 2 },
  nextLabel: { color: C.textSecondary, fontSize: 12, fontWeight: '600' },
  nextText: { color: C.textStrong, fontSize: 13 },
  actions: { flexDirection: 'row', flexWrap: 'wrap', gap: Spacing.three, marginTop: Spacing.one },
  link: { color: C.washuRed, fontSize: 12, fontWeight: '600' },
  detail: {
    marginTop: Spacing.one,
    padding: Spacing.two,
    borderRadius: Radius.control,
    backgroundColor: C.surfaceDark,
    color: C.textOnDark,
    fontFamily: Fonts?.mono,
    fontSize: 11,
    lineHeight: 15,
  },
});

import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import * as Linking from 'expo-linking';
import { apiClient, type RunFailure, type RunFiles } from '@/api/client';
import { Colors, Fonts, Radius, Spacing } from '@/constants/theme';

const C = Colors.light;

// Lines of job output shown before "Show all".
const OUTPUT_PREVIEW_LINES = 8;

// The RIS API Portal has a jobs page but no per-job route.
const RIS_PORTAL_JOBS = 'https://d3n2m687w2hvtj.cloudfront.net/jobs';

// Where each cluster outcome is best re-run from (the card's one-click action).
const RERUN_FROM: Record<string, { state: string; label: string }> = {
  failed: { state: 'BUILD', label: 'Re-run from BUILD' },
  dependency_error: { state: 'PLAN', label: 'Re-run from PLAN' },
  timeout: { state: 'PLAN', label: 'Re-run from PLAN' },
};

interface Props {
  failure: RunFailure;
  /** The run, for its download links (cluster failures only). */
  conversationId?: string;
  /** Open the re-run editor at a stage (ChatScreen's "Re-run from…"). */
  onRerun?: (state: string) => void;
}

/** A job's output stream, its last lines open, the rest behind "Show all". */
function JobOutput({ label, text }: { label: string; text: string }) {
  const [all, setAll] = useState(false);
  const lines = text.split('\n');
  const hidden = all ? 0 : Math.max(0, lines.length - OUTPUT_PREVIEW_LINES);
  return (
    <View style={styles.next}>
      <Text style={styles.nextLabel}>{label}</Text>
      <Text style={styles.detail} selectable accessibilityLabel={label}>
        {lines.slice(hidden).join('\n')}
      </Text>
      {lines.length > OUTPUT_PREVIEW_LINES ? (
        <Pressable onPress={() => setAll((o) => !o)} accessibilityRole="button"
          accessibilityState={{ expanded: all }}>
          <Text style={styles.link}>{all ? 'Show the last lines only' : `Show all ${lines.length} lines`}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

/** The commands that re-run this attempt by hand on RIS, in the env the job used. */
export function reproduceCommand(failure: RunFailure, conversationId: string, bundleUrl: string): string {
  const env = failure.env || 'default';
  const dir = `~/twain-debug/${conversationId.split('-')[0]}-a${failure.attempt ?? 1}`;
  return [
    failure.env_file ? `. ${failure.env_file}` : '. <your twain.sh>   # sets TWAIN_ENVS_ROOT',
    `mkdir -p ${dir} && cd ${dir}`,
    `curl -fsSL '${bundleUrl}' | tar xz`,
    `P="$TWAIN_ENVS_ROOT/${env}"; export PATH="$P/bin:$PATH" CONDA_PREFIX="$P"${failure.env ? '' : '   # env not recorded; default shown'}`,
    'for h in "$P"/etc/conda/activate.d/*.sh; do . "$h"; done',
    'python main.py',
  ].join('\n');
}

/**
 * Where a run stopped, why in plain words, and what to do next.
 *
 * Replaces the bare "The run failed — see the run log for details." with the
 * failure the runner already classified (a `run.error` event's `failure`):
 * the stage, a one-line headline, the exception, the next step, and the full
 * error behind "Show details". Modelled on RETICLE's ETL "Why it stopped" panel.
 *
 * For a cluster job, everything needed to act is on the card: its stdout and
 * stderr (open by default -- a traceback lands in stdout, the wrapper's
 * "payload exited N" in stderr), download links to the run bundle and outputs
 * in S3, the commands that reproduce it on RIS in the env the job used, and a
 * one-click re-run. Never a path inside the worker container, which a
 * researcher can't reach (run cb1a625e).
 */
export function FailureCard({ failure, conversationId, onRerun }: Props) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<RunFiles | null>(null);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [showReproduce, setShowReproduce] = useState(false);
  const cluster = Boolean(failure.job_id && conversationId);
  const rerun = onRerun && failure.outcome ? RERUN_FROM[failure.outcome] : undefined;

  // Links are short-lived: fetch them when asked for, not when the card renders.
  const loadFiles = async (): Promise<RunFiles | null> => {
    if (!conversationId) return null;
    try {
      const fresh = await apiClient.getRunFiles(conversationId, failure.attempt);
      setFiles(fresh);
      setFilesError(null);
      return fresh;
    } catch {
      setFilesError('Could not get download links for this run.');
      return null;
    }
  };
  const download = async (which: 'bundle' | 'outputs') => {
    const url = (await loadFiles())?.urls[which];
    if (url) void Linking.openURL(url);
    else setFilesError(which === 'outputs'
      ? 'This attempt uploaded no outputs (it stopped before its upload step).'
      : 'This attempt has no run bundle in storage.');
  };
  const toggleReproduce = async () => {
    if (!showReproduce) await loadFiles();
    setShowReproduce((o) => !o);
  };

  return (
    <View style={styles.card} accessibilityRole="alert">
      <Text style={styles.kicker}>
        {`Stopped while ${failure.stage_label.toLowerCase()} · ${failure.stage}`}
      </Text>
      <Text style={styles.headline}>{failure.headline}</Text>
      {failure.exception ? (
        <Text style={styles.exception} selectable>{failure.exception}</Text>
      ) : failure.cause ? <Text style={styles.cause}>{failure.cause}</Text> : null}

      {failure.next_step ? (
        <View style={styles.next}>
          <Text style={styles.nextLabel}>What to do next</Text>
          <Text style={styles.nextText}>{failure.next_step}</Text>
        </View>
      ) : null}

      {cluster || rerun ? (
        <View style={styles.buttons}>
          {rerun ? (
            <Pressable style={[styles.button, styles.buttonPrimary]} accessibilityRole="button"
              onPress={() => onRerun?.(rerun.state)}>
              <Text style={styles.buttonPrimaryText}>{rerun.label}</Text>
            </Pressable>
          ) : null}
          {cluster ? (
            <>
              <Pressable style={styles.button} accessibilityRole="button" onPress={() => void download('bundle')}>
                <Text style={styles.buttonText}>Download run bundle</Text>
              </Pressable>
              <Pressable style={styles.button} accessibilityRole="button" onPress={() => void download('outputs')}>
                <Text style={styles.buttonText}>Download outputs</Text>
              </Pressable>
              <Pressable style={styles.button} accessibilityRole="button"
                accessibilityState={{ expanded: showReproduce }} onPress={() => void toggleReproduce()}>
                <Text style={styles.buttonText}>{showReproduce ? 'Hide RIS commands' : 'Reproduce on RIS'}</Text>
              </Pressable>
            </>
          ) : null}
        </View>
      ) : null}
      {filesError ? <Text style={styles.cause}>{filesError}</Text> : null}

      {showReproduce && conversationId ? (
        files?.urls.bundle ? (
          <View style={styles.next}>
            <Text style={styles.nextLabel}>
              {`On a RIS login node — the link expires in ${Math.round(files.expires_in / 60)} min; reopen for a fresh one`}
            </Text>
            <Text style={styles.detail} selectable accessibilityLabel="Commands to reproduce on RIS">
              {reproduceCommand(failure, conversationId, files.urls.bundle)}
            </Text>
          </View>
        ) : files ? <Text style={styles.cause}>This attempt has no run bundle in storage.</Text> : null
      ) : null}

      {failure.self_heal?.length ? (
        // Each attempt's diagnosis and what came of it, so "it failed" also says
        // what was already tried and why TWAIN stopped where it did.
        <View style={styles.next}>
          <Text style={styles.nextLabel}>What TWAIN tried</Text>
          {failure.self_heal.map((step) => (
            <Text key={`${step.attempt}-${step.class}`} style={styles.healStep}>
              {`Attempt ${step.attempt} · ${step.class}: ${step.reason} → ${step.result}`}
            </Text>
          ))}
        </View>
      ) : null}

      {failure.job_stdout ? (
        <JobOutput label={failure.job_id ? `Job output (stdout) · Slurm job ${failure.job_id}` : 'Job output (stdout)'}
          text={failure.job_stdout} />
      ) : null}
      {failure.job_stderr ? (
        <JobOutput label={failure.job_id ? `Job output (stderr) · Slurm job ${failure.job_id}` : 'Job output (stderr)'}
          text={failure.job_stderr} />
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
  exception: { color: C.washuRed, fontSize: 12, fontFamily: Fonts?.mono },
  next: { marginTop: Spacing.one, gap: 2 },
  nextLabel: { color: C.textSecondary, fontSize: 12, fontWeight: '600' },
  nextText: { color: C.textStrong, fontSize: 13 },
  healStep: { color: C.textStrong, fontSize: 12, lineHeight: 17 },
  buttons: { flexDirection: 'row', flexWrap: 'wrap', gap: Spacing.two, marginTop: Spacing.two },
  button: {
    paddingVertical: Spacing.one + 2,
    paddingHorizontal: Spacing.three,
    borderRadius: Radius.control,
    borderWidth: 1,
    borderColor: C.washuRed,
  },
  buttonText: { color: C.washuRed, fontSize: 12, fontWeight: '600' },
  buttonPrimary: { backgroundColor: C.washuRed },
  buttonPrimaryText: { color: C.washuWhite, fontSize: 12, fontWeight: '700' },
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

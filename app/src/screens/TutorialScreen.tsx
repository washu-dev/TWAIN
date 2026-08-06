import React, { useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  TouchableOpacity,
  ActivityIndicator,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { Footer, Header, PlanCard, StateStepper, WallTimeField } from '@/components';
import { APP_STRINGS, Colors, Spacing } from '@/constants/theme';
import { useAuth } from '@/hooks/useAuth';
import { DurationUnit } from '@/utils/duration';

const C = Colors.light;

/**
 * A worked example, used by every section that needs a plan to point at.
 *
 * Deliberately a real plan artifact rather than prose describing one: PlanCard
 * takes the runner's own JSON, so the card below is the card you will actually be
 * asked to approve, not a drawing of it. The numbers are from a genuine CO2
 * heat-of-formation run.
 */
const EXAMPLE_PLAN = JSON.stringify({
  compute_target: 'slurm',
  slurm_cluster: 'compute2',
  summary:
    'Compute the standard heat of formation of CO2 with Psi4, via an '
    + 'error-cancelling reaction (CO + 1/2 O2 -> CO2) so the method’s per-bond '
    + 'errors cancel between the two sides.',
  requested_property: 'standard_heat_of_formation',
  target_system: { formula: 'CO2', kind: 'molecule' },
  selected_method: { tool_name: 'Psi4', tool_version: '1.9', libraries: ['Psi4'] },
  cost_estimate: { min_cost: 0.21 },
  compute_estimate: { cpu_hours: 1.67 },
  slurm_request: { cpu_count: 10, gpu_count: 0, ram: 16, max_time: 0.16666666666666666 },
  acceptance_metrics: [
    { metric_name: 'standard_heat_of_formation', target_value: -393.5, tolerance: 10 },
  ],
  safety_notes: ['Reaction route chosen so per-bond errors cancel.'],
});

interface Topic {
  id: string;
  title: string;
  /** One line, shown collapsed. Should be useful on its own. */
  summary: string;
  body: string[];
  /** Rendered under the prose. The real component wherever one exists. */
  visual?: React.ReactNode;
  /** What the visual is, for a screen reader and for anyone it does not render for. */
  visualCaption?: string;
}

export const TutorialScreen: React.FC = () => {
  const router = useRouter();
  const { signOut } = useAuth();
  // Which sections are open. Collapsed by default: twelve features expanded is a
  // manual, and the point of the contents list is to let someone read one thing.
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // Live state for the wall-time demo, so the section about it is the control
  // itself rather than a picture of the control.
  const [demoAmount, setDemoAmount] = useState('10');
  const [demoUnit, setDemoUnit] = useState<DurationUnit>('m');

  const toggle = (id: string) => setOpen((prev) => ({ ...prev, [id]: !prev[id] }));

  const TOPICS: Topic[] = [
    {
      id: 'pipeline',
      title: 'The eleven stages of a run',
      summary: 'What TWAIN does between your sentence and your answer.',
      body: [
        'You describe a calculation in plain English. TWAIN turns that into a '
        + 'plan, writes the code, runs it on the WashU RIS cluster, reads the '
        + 'output back, and checks the answer against the literature. Every run '
        + 'walks the same eleven stages, and the strip below shows which one it '
        + 'is in.',
        'INTAKE reads your request. CLARIFY asks you about anything ambiguous. '
        + 'DECOMPOSE breaks the goal into steps. DISCOVER picks candidate tools. '
        + 'PLAN commits to a method, a system and a resource request — and stops '
        + 'for your approval. BUILD writes the script. EXECUTE submits it to '
        + 'Slurm. INTERPRET parses the numbers out of the output. VALIDATE '
        + 'compares them to references. ACCEPT and TERMINATE close the run.',
        'Three more stages exist off this line: REPAIR fixes a generated script '
        + 'before it runs, and CORRECT and REPLAN handle a result that did not '
        + 'validate. They are loops, not steps, which is why the strip does not '
        + 'show them.',
      ],
      visual: <StateStepper current="EXECUTE" />,
      visualCaption:
        'The stage strip, showing a run part-way through EXECUTE: finished stages '
        + 'green, the current one red.',
    },
    {
      id: 'starting',
      title: 'Starting a run',
      summary: 'Describe the calculation. Set a spend cap if you want one.',
      body: [
        'Open Start a simulation and type what you want computed — for example '
        + '“what is the standard heat of formation of carbon dioxide?” or '
        + '“predict the band gap of silicon”. Name the property and the system; '
        + 'you do not need to name a method, an engine, or a basis set. Choosing '
        + 'those is TWAIN’s job, and it will tell you what it chose before '
        + 'anything runs.',
        'The Budget field caps what the run may spend on language-model calls, in '
        + 'dollars. Leave it blank for the deployment default. It does not cap '
        + 'cluster time — the wall-time limit on the approval card does that.',
        'Being specific helps. “Use GPAW” or “at 500 eV cutoff” will be honoured '
        + 'if it is possible on this cluster; if it is not, the plan says so '
        + 'instead of quietly substituting something else.',
      ],
    },
    {
      id: 'clarify',
      title: 'Clarifying questions',
      summary: 'TWAIN asks rather than assuming, and waits for you.',
      body: [
        'If your request leaves something open that changes the answer — a phase, '
        + 'a temperature, which polymorph — TWAIN asks in the chat and the run '
        + 'pauses at CLARIFY until you reply. The run is not consuming cluster '
        + 'time while it waits.',
        'You can answer in plain English, and you can say “either is fine” — the '
        + 'question exists because the choice matters, so an explicit “you pick” '
        + 'is more useful than a guess.',
      ],
    },
    {
      id: 'approval',
      title: 'The approval gate',
      summary: 'Nothing runs on the cluster until you say so.',
      body: [
        'At PLAN, TWAIN stops and shows you exactly what it intends to do. Read '
        + 'the summary first: it says the method in words. Then check Method and '
        + 'System — those are the two that make an answer wrong rather than '
        + 'merely imprecise.',
        'Estimated cost is language-model dollars plus CPU-hours. Slurm ask is '
        + 'the resources the job will request. Accept if is the tolerance the '
        + 'result will be judged against later; it can be blank, which means '
        + 'there is nothing to judge against and the run will say so rather than '
        + 'pretend otherwise.',
        'Notes carry anything TWAIN wants you to know before you commit — '
        + 'including when the best-fit engine is not installed here and the plan '
        + 'uses a substitute. Approve, or reject and say what to change.',
      ],
      visual: <PlanCard content={EXAMPLE_PLAN} />,
      visualCaption: 'A real approval card for a CO2 heat-of-formation run.',
    },
    {
      id: 'resources',
      title: 'Resources and wall time',
      summary: 'The suggested figures are editable. This is the control, live.',
      body: [
        'The CPU, GPU, RAM and wall-time figures on the approval card are '
        + 'TWAIN’s suggestion, not a requirement — roughly one CPU per atom, '
        + 'and a wall time from the size of the job. Change any of them before '
        + 'approving. Each field states the cluster’s ceiling, and an over-ask '
        + 'is clamped to it rather than producing a job Slurm will never schedule.',
        'Wall time is the one to think about. It is a KILL time, not a booking: '
        + 'the job is stopped when it expires, so too low loses the work and too '
        + 'high only means a longer queue wait. Type the number and pick the '
        + 'unit — the control below is the real one, so try it.',
        'Slurm itself only accepts hours, and the plan stores hours. You never '
        + 'have to see that: enter 10 minutes and TWAIN converts.',
      ],
      visual: (
        <View style={styles.demoBox}>
          <WallTimeField
            label="Wall time — max 168h"
            value={demoAmount}
            unit={demoUnit}
            onChange={(value, unit) => {
              setDemoAmount(value);
              setDemoUnit(unit);
            }}
          />
          <Text style={styles.demoNote}>
            {`Stored in the plan as ${
              demoUnit === 'm'
                ? `${(Number(demoAmount || 0) / 60).toFixed(4)} hours`
                : `${Number(demoAmount || 0)} hours`
            } — the unit Slurm takes.`}
          </Text>
        </View>
      ),
      visualCaption:
        'A working wall-time field: type a number, tap min or hours, and see the '
        + 'value the plan will store.',
    },
    {
      id: 'progress',
      title: 'Watching a run',
      summary: 'Live stage, live clock against the wall-time cap, and a stop button.',
      body: [
        'While a run is going, the stage strip advances and a spinner sits under '
        + 'the last message with the running time beside it — “13m 01s / 10m” is '
        + 'thirteen minutes elapsed against a ten-minute cap. A multi-hour DFT '
        + 'job and a hung one look identical without that clock.',
        'Terminate stops the run and cancels the Slurm job. A terminated run '
        + 'keeps everything it produced up to that point, and shows as '
        + '“terminated” in Browse rather than as a failure.',
        'You can close the app. The run continues on the cluster, and TWAIN emails '
        + 'you when it needs you or finishes — see Notifications below.',
      ],
      visual: (
        <View style={styles.demoBox}>
          <StateStepper current="EXECUTE" />
          <View style={styles.workingRow}>
            <ActivityIndicator color={C.washuRed} />
            <Text style={styles.workingText}>Working…</Text>
            <Text style={styles.workingElapsed}>13m 01s / 10m</Text>
          </View>
        </View>
      ),
      visualCaption:
        'A run in EXECUTE, with the working spinner and elapsed time against the cap.',
    },
    {
      id: 'results',
      title: 'Reading the results',
      summary: 'The number, how it was judged, and every file behind it.',
      body: [
        'When a run finishes, View results opens the report: the primary metric '
        + 'with its uncertainty, the validation verdict and why, the budget and '
        + 'elapsed time, and which Slurm job produced it.',
        'Below that is every artifact the run wrote — the generated script, the '
        + 'engine input and output files, the results table, the raw log. Nothing '
        + 'is hidden: if you want to check the number yourself, the whole '
        + 'calculation is there to inspect.',
        'The report streams while the run is still going, so you can watch a long '
        + 'job’s output arrive rather than waiting for the end.',
      ],
    },
    {
      id: 'validation',
      title: 'What “accepted” means',
      summary: 'Agreement with a reference — or an honest statement that there was none.',
      body: [
        'VALIDATE compares your result to a literature baseline and to the '
        + 'tolerance in the plan. Within 15% relative error is accepted, within '
        + '30% needs review, worse is rejected. Those thresholds are '
        + 'configurable per run.',
        'Read the rationale, not just the verdict. “Accepted” can also mean no '
        + 'baseline covered your system and the plan set no target, so nothing '
        + 'could grade the number — the report says exactly that, and it is not '
        + 'the same as agreement.',
        'A value that is not physically possible is rejected on those grounds '
        + 'alone, with no reference needed: a formation enthalpy of '
        + '−27,452 kJ/mol is not an inaccurate answer, it is not a formation '
        + 'enthalpy. When that fires, the report names the unit slips that would '
        + 'explain it.',
      ],
    },
    {
      id: 'rerun',
      title: 'Re-running from a stage',
      summary: 'Change one thing and redo only what depends on it.',
      body: [
        'Re-run from… on a finished run lets you restart at any stage. That stage '
        + 'and everything after it run again; the work before it is kept as '
        + 'input. Re-running from EXECUTE reuses the approved plan and the '
        + 'generated script. Re-running from INTAKE re-reads your request from '
        + 'scratch.',
        'Each stage takes a note — “use a larger basis set”, “the geometry looked '
        + 'wrong” — which is folded into the intent for that stage. From INTAKE '
        + 'you can edit the original request directly.',
        'Resources are editable when you re-run from BUILD or later, because the '
        + 'approved plan survives. Earlier than that a fresh plan would overwrite '
        + 'them, so the fields are not offered and the API refuses them.',
      ],
    },
    {
      id: 'browse',
      title: 'Browse',
      summary: 'Every run you have started, with live status.',
      body: [
        'Browse lists your runs newest first, with the stage each one reached and '
        + 'its status: running, awaiting your input, awaiting approval, '
        + 'completed, failed, terminated or cancelled. Running rows show elapsed '
        + 'time and refresh on their own — you do not need to pull to reload.',
        'Tap any run to reopen it. A run awaiting approval or a question picks up '
        + 'exactly where it paused, however long ago that was.',
      ],
    },
    {
      id: 'libraries',
      title: 'Libraries and engines',
      summary: 'What this cluster can actually run, probed rather than promised.',
      body: [
        'The Libraries screen lists every engine and library TWAIN knows about, '
        + 'and whether it is installed on this cluster — with the environment '
        + 'that provides it, so a claim can be checked.',
        'The list is not hand-maintained. The runner probes the cluster '
        + 'environments when it starts and publishes what it found: engines by '
        + 'their binary, libraries by import. It errs toward under-claiming — '
        + 'something absent from a provisioned environment is listed as '
        + 'unavailable even if a job could install it on the fly.',
        'If your calculation needs something that is not there, Report an issue '
        + 'files a provisioning request. The approval card also flags it in Notes '
        + 'when the best-fit engine for your request is missing.',
      ],
    },
    {
      id: 'notifications',
      title: 'Notifications and reporting issues',
      summary: 'Email when a run needs you. One tap to file a bug.',
      body: [
        'Settings controls which emails you get: a run needs your input, a plan '
        + 'is ready to approve, a run completed, failed or was terminated. There '
        + 'is a master switch, and each event can be turned off on its own. Runs '
        + 'take hours, so this is how you find out without watching.',
        'Report an issue files a GitHub issue from inside the app. From a run '
        + 'window it attaches that run’s identity, so a report about a specific '
        + 'failure carries the run it happened in.',
        'Settings also holds a diagnostics check that confirms this app can reach '
        + 'the TWAIN backend — worth trying first if a screen will not load.',
      ],
    },
  ];

  return (
    <SafeAreaView style={styles.container} edges={['left', 'right', 'bottom']}>
      <Header onLoginPress={signOut} loginLabel={APP_STRINGS.signOutButton} />

      <ScrollView
        style={styles.scroll}
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={Platform.OS !== 'web'}
      >
        <TouchableOpacity
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/dashboard'))}
          accessibilityRole="button"
          style={styles.backButton}
        >
          <Text style={styles.backText}>‹ Back to dashboard</Text>
        </TouchableOpacity>

        <Text style={styles.pageTitle}>How TWAIN works</Text>
        <Text style={styles.intro}>
          Twelve features, each with the reasoning behind it. Tap one to open it.
          The examples are the real screens — the approval card and the wall-time
          control below are the components you will use, not pictures of them.
        </Text>

        {TOPICS.map((topic, index) => {
          const isOpen = !!open[topic.id];
          return (
            <View key={topic.id} style={styles.section}>
              <TouchableOpacity
                style={styles.sectionHeader}
                onPress={() => toggle(topic.id)}
                accessibilityRole="button"
                aria-expanded={isOpen}
                accessibilityLabel={topic.title}
                accessibilityHint={topic.summary}
              >
                <Text style={styles.sectionNumber}>{index + 1}</Text>
                <View style={styles.sectionHeading}>
                  <Text style={styles.sectionTitle}>{topic.title}</Text>
                  <Text style={styles.sectionSummary}>{topic.summary}</Text>
                </View>
                <Text style={styles.chevron}>{isOpen ? '−' : '+'}</Text>
              </TouchableOpacity>

              {isOpen && (
                <View style={styles.sectionBody}>
                  {topic.body.map((paragraph, i) => (
                    <Text key={`${topic.id}-p${i}`} style={styles.paragraph}>
                      {paragraph}
                    </Text>
                  ))}
                  {topic.visual ? (
                    <View style={styles.visual}>
                      {topic.visual}
                      {topic.visualCaption ? (
                        <Text style={styles.caption}>{topic.visualCaption}</Text>
                      ) : null}
                    </View>
                  ) : null}
                </View>
              )}
            </View>
          );
        })}

        <TouchableOpacity
          style={styles.cta}
          onPress={() => router.push('/chat')}
          accessibilityRole="button"
        >
          <Text style={styles.ctaText}>Start a simulation →</Text>
        </TouchableOpacity>
      </ScrollView>

      <Footer />
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: C.washuLightGray },
  scroll: { flex: 1 },
  content: {
    flexGrow: 1,
    paddingHorizontal: Spacing.four,
    paddingTop: Spacing.four,
    paddingBottom: Spacing.four,
    maxWidth: 720,
    width: '100%',
    alignSelf: 'center',
  },
  backButton: { alignSelf: 'flex-start', marginBottom: Spacing.two },
  backText: { color: C.washuRed, fontSize: 14, fontWeight: '600' },
  pageTitle: {
    fontSize: 24,
    fontWeight: '700',
    color: C.textStrong,
    marginBottom: Spacing.two,
  },
  intro: {
    fontSize: 14,
    color: C.textSecondary,
    lineHeight: 21,
    marginBottom: Spacing.three,
  },
  section: {
    backgroundColor: C.background,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: C.border,
    marginBottom: Spacing.two,
    overflow: 'hidden',
  },
  sectionHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    padding: Spacing.three,
  },
  sectionNumber: {
    fontSize: 13,
    fontWeight: '700',
    color: C.washuGreen,
    minWidth: 18,
    textAlign: 'center',
  },
  sectionHeading: { flex: 1, gap: 2 },
  sectionTitle: { fontSize: 16, fontWeight: '700', color: C.textStrong },
  sectionSummary: { fontSize: 12, color: C.textSecondary, lineHeight: 17 },
  chevron: { fontSize: 20, color: C.washuRed, fontWeight: '700', paddingHorizontal: Spacing.one },
  sectionBody: {
    paddingHorizontal: Spacing.three,
    paddingBottom: Spacing.three,
    gap: Spacing.two,
    borderTopWidth: 1,
    borderTopColor: C.divider,
    paddingTop: Spacing.three,
  },
  paragraph: { fontSize: 14, color: C.text, lineHeight: 21 },
  visual: {
    marginTop: Spacing.two,
    gap: Spacing.two,
    padding: Spacing.two,
    backgroundColor: C.washuLightGray,
    borderRadius: 6,
  },
  caption: { fontSize: 11, color: C.textSecondary, fontStyle: 'italic', lineHeight: 16 },
  demoBox: { gap: Spacing.two },
  demoNote: { fontSize: 12, color: C.textSecondary, fontVariant: ['tabular-nums'] },
  workingRow: { flexDirection: 'row', alignItems: 'center', gap: Spacing.two, padding: Spacing.two },
  workingText: { color: C.textSecondary, fontSize: 13 },
  workingElapsed: { color: C.textSecondary, fontSize: 13, fontVariant: ['tabular-nums'] },
  cta: {
    marginTop: Spacing.three,
    backgroundColor: C.washuGreen,
    borderRadius: 6,
    paddingVertical: Spacing.three,
    alignItems: 'center',
  },
  ctaText: { color: C.washuWhite, fontSize: 15, fontWeight: '700' },
});

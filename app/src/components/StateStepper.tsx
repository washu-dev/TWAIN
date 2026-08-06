import React from 'react';
import { View, Text, ScrollView, StyleSheet } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

/**
 * The happy-path pipeline, in order. Loop states (CORRECT, REPLAN, REPAIR) are
 * deliberately absent: they are detours off this spine, not extra steps along it,
 * and drawing them would suggest every run passes through them.
 */
export const PIPELINE_STATES = [
  'INTAKE', 'CLARIFY', 'DECOMPOSE', 'DISCOVER', 'PLAN',
  'BUILD', 'EXECUTE', 'INTERPRET', 'VALIDATE', 'ACCEPT', 'TERMINATE',
];

interface StateStepperProps {
  /** The stage the run is in. Anything not in PIPELINE_STATES leaves every dot grey. */
  current: string;
  /** ``completed`` fills every dot: a finished run is past all of them. */
  status?: string;
}

/**
 * Where a run has got to.
 *
 * Lives here rather than inside ChatScreen because the tutorial explains the
 * pipeline by SHOWING this component with a stage selected -- so the explanation
 * cannot drift from the thing it explains. That only works while there is one
 * definition, which is the point of the move.
 */
export const StateStepper: React.FC<StateStepperProps> = ({ current, status }) => {
  const currentIndex = PIPELINE_STATES.indexOf(current);
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      style={styles.stepper}
      contentContainerStyle={styles.stepperContent}
    >
      {PIPELINE_STATES.map((state, i) => {
        const done = currentIndex > i || status === 'completed';
        const active = currentIndex === i && status !== 'completed';
        return (
          <View key={state} style={styles.step}>
            <View style={[styles.dot, done && styles.dotDone, active && styles.dotActive]} />
            <Text style={[styles.stepLabel, active && styles.stepLabelActive]}>{state}</Text>
          </View>
        );
      })}
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  stepper: { maxHeight: 62, backgroundColor: C.backgroundElement, flexGrow: 0 },
  stepperContent: { alignItems: 'center', paddingHorizontal: Spacing.three, gap: Spacing.three },
  step: { alignItems: 'center', gap: 4, paddingVertical: Spacing.two },
  dot: { width: 12, height: 12, borderRadius: 6, backgroundColor: C.borderStrong },
  dotDone: { backgroundColor: C.washuGreen },
  dotActive: { backgroundColor: C.washuRed, transform: [{ scale: 1.3 }] },
  stepLabel: { fontSize: 9, color: C.textSecondary },
  stepLabelActive: { color: C.washuRed, fontWeight: '700' },
});

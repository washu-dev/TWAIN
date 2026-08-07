import React from 'react';
import { StyleProp, StyleSheet, Text, ViewStyle } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { PressableScale } from './Motion';
import { Colors, Elevation, Gradients, Radius, Spacing } from '@/constants/theme';

const C = Colors.light;

interface PrimaryButtonProps {
  label: string;
  onPress: () => void;
  /** Greys the fill AND blocks the press. Both, or neither -- see the note below. */
  disabled?: boolean;
  /** `calm` (green) for go-ahead, `action` (crimson) for the louder ask. */
  tone?: 'calm' | 'action';
  accessibilityLabel?: string;
  style?: StyleProp<ViewStyle>;
}

/**
 * The one definition of "the primary action button".
 *
 * This exists because it did NOT exist. A gradient fill cannot live on the same
 * view as the rounding that clips it, so the button became two nodes: a wrapper
 * carrying flex/radius/overflow/shadow, and an inner gradient carrying the fill.
 * That split was applied to the ONE call site being looked at, and the shared
 * `approveBtn` style it left behind -- now only padding and alignment, with no
 * background -- stayed on three others: "Yes, run it", "Accept this result", and
 * "Re-run with this". White text, no fill, no flex: the primary action rendered
 * as invisible text at three different gates, including the one that ends a run.
 *
 * A shared style cannot enforce a required wrapper; a component can. The
 * structure is now unforgeable at the call site, which is the actual fix -- the
 * three broken buttons were a symptom of the button having no single owner.
 *
 * `disabled` deliberately drives both the dimming and the press blocking. The
 * old call sites dimmed to 50% while staying fully pressable, so a researcher
 * who tapped a greyed "Approve & submit to RIS" twice submitted the job twice.
 */
export const PrimaryButton: React.FC<PrimaryButtonProps> = ({
  label,
  onPress,
  disabled = false,
  tone = 'calm',
  accessibilityLabel,
  style,
}) => (
  <PressableScale
    style={[styles.wrap, disabled && styles.disabled, style]}
    onPress={onPress}
    disabled={disabled}
    accessibilityRole="button"
    accessibilityLabel={accessibilityLabel ?? label}
    accessibilityState={{ disabled }}
  >
    <LinearGradient
      colors={Gradients[tone]}
      start={{ x: 0, y: 0 }}
      end={{ x: 1, y: 1 }}
      style={styles.fill}
    >
      <Text style={styles.label}>{label}</Text>
    </LinearGradient>
  </PressableScale>
);

const styles = StyleSheet.create({
  // Layout, rounding and shadow live here; the gradient inside carries colour.
  // They cannot be one view -- a gradient child renders square inside a rounded
  // parent unless the parent clips it.
  wrap: {
    flex: 1,
    borderRadius: Radius.control,
    overflow: 'hidden',
    boxShadow: Elevation.card,
  },
  // `flex: 1` so the gradient fills the wrapper's HEIGHT, not just its own
  // content's. The wrapper stretches to the tallest child of the button row, and
  // the outlined sibling it sits next to is always exactly 2px taller (same
  // padding and type, plus a 1px border top and bottom). Without this the
  // gradient stops short, `overflow: 'hidden'` clips it on a flat line, and the
  // button's bottom corners lose their rounding against a transparent strip.
  fill: {
    flex: 1,
    paddingVertical: Spacing.three,
    alignItems: 'center',
    justifyContent: 'center',
  },
  label: { color: C.washuWhite, fontWeight: '700', fontSize: 15 },
  disabled: { opacity: 0.5 },
});

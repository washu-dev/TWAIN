import React from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet } from 'react-native';
import { Colors, Radius, Spacing } from '@/constants/theme';
import { DurationUnit, formatDurationHours } from '@/utils/duration';

const C = Colors.light;

/**
 * Shortest wall time this field will submit. Anything below it is raised.
 *
 * Lives with the field rather than with the screen that clamps, so the number
 * enforced on submit and the number printed on the label cannot disagree.
 */
export const MIN_WALL_HOURS = 10 / 60;

/**
 * The field's label: "Wall time — 10m–168h", or just the floor when the ceiling
 * is unknown.
 *
 * Generated rather than written out, because a hand-written one goes stale. The
 * tutorial carried `label="Wall time — max 168h"` while the real field had begun
 * showing its minimum too -- on a screen whose whole premise is that the example
 * IS the control, not a picture of it, and which is the only place a researcher
 * is told what the field means.
 */
export const wallTimeLabel = (maxHours?: number): string =>
  maxHours != null
    ? `Wall time — ${formatDurationHours(MIN_WALL_HOURS)}–${formatDurationHours(maxHours)}`
    : `Wall time — min ${formatDurationHours(MIN_WALL_HOURS)}`;

interface WallTimeFieldProps {
  label: string;
  /** The number only. `unit` says what it counts. */
  value: string;
  unit: DurationUnit;
  onChange: (value: string, unit: DurationUnit) => void;
}

/**
 * Wall time: a number, plus a control that says what the number counts.
 *
 * The unit used to be typed into the value ("90m", "1.5h"), which is unreachable
 * on a phone -- a numeric field brings up a keypad with no letters, so the only
 * thing enterable there was a bare number, which meant HOURS: asking for ten
 * minutes got ten hours, or 0.1666 if you did the division by hand. Two controls,
 * so neither the keyboard nor the reader has to guess.
 *
 * Shared with the tutorial, which explains this field by rendering a working copy
 * of it. An explanation that is the component cannot describe an older version of
 * it by mistake.
 */
export const WallTimeField: React.FC<WallTimeFieldProps> = ({
  label,
  value,
  unit,
  onChange,
}) => (
  <View style={styles.field}>
    <Text style={styles.label}>{label}</Text>
    <View style={styles.row}>
      <TextInput
        style={[styles.input, styles.amount]}
        value={value}
        onChangeText={(v) => onChange(v, unit)}
        keyboardType="decimal-pad"
        accessibilityLabel={`${label}, amount`}
      />
      {/* radio, not button: a one-of-two choice, and the role is what carries the
          state to a screen reader. Written with the ARIA props rather than
          accessibilityState because that is what react-native-web 0.21 forwards
          (checked verified in the DOM) -- accessibilityState={{selected}} on a
          button emitted NOTHING, leaving the red fill as the only cue for which
          unit was active. RN 0.85 maps aria-checked to native state, so this is
          the portable spelling, not a web-only patch. */}
      <View style={styles.unitToggle} role="radiogroup">
        {(['m', 'h'] as DurationUnit[]).map((option) => {
          const active = unit === option;
          return (
            <TouchableOpacity
              key={option}
              style={[styles.unitOption, active && styles.unitOptionActive]}
              onPress={() => onChange(value, option)}
              role="radio"
              aria-checked={active}
              aria-label={option === 'm' ? 'minutes' : 'hours'}
            >
              <Text style={[styles.unitOptionText, active && styles.unitOptionTextActive]}>
                {option === 'm' ? 'min' : 'hours'}
              </Text>
            </TouchableOpacity>
          );
        })}
      </View>
    </View>
  </View>
);

const styles = StyleSheet.create({
  field: { flex: 1, gap: 4 },
  label: { fontSize: 11, color: C.textSecondary, fontWeight: '600' },
  input: {
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: Radius.control,
    paddingHorizontal: Spacing.two,
    paddingVertical: Spacing.one,
    backgroundColor: C.washuWhite,
    fontSize: 14,
    color: C.text,
  },
  row: { flexDirection: 'row', alignItems: 'center', gap: Spacing.one },
  amount: { flex: 1 },
  unitToggle: {
    flexDirection: 'row',
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: Radius.control,
    overflow: 'hidden',
    backgroundColor: C.washuWhite,
  },
  unitOption: { paddingHorizontal: Spacing.two, paddingVertical: Spacing.one },
  unitOptionActive: { backgroundColor: C.washuRed },
  unitOptionText: { fontSize: 12, color: C.textSecondary, fontWeight: '600' },
  unitOptionTextActive: { color: C.washuWhite },
});

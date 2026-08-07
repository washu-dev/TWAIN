/**
 * Duration formatting and parsing shared by the run screens.
 *
 * Kept as pure functions (no React, no Date.now inside) so they can be reasoned
 * about and exercised directly: the app has no JS test runner, so anything with
 * hidden time state would be untestable by construction.
 */

/**
 * A compact running-time label, in the shape a person reads at a glance.
 *
 *   9  -> "9s"        (sub-minute: seconds only)
 *   75 -> "1m 15s"    (under an hour: minutes and seconds)
 *   3725 -> "1h 02m"  (over an hour: seconds stop mattering and would only churn)
 *
 * Negative or non-finite input clamps to 0s rather than rendering "NaN": clock
 * skew between the API host and the browser can make a fresh start look
 * fractionally in the future.
 */
export function formatElapsed(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) totalSeconds = 0;
  const s = Math.floor(totalSeconds);
  if (s < 60) return `${s}s`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return `${minutes}m ${String(s % 60).padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/**
 * Parse a wall-time entry into HOURS, which is the unit the plan stores.
 *
 * Accepts what a researcher actually types for a limit -- "90m", "45 min",
 * "1.5h", "2 hours" -- because expressing a 20-minute cap as "0.33" is a
 * pointless conversion to do in your head, and getting it wrong silently buys
 * the wrong allocation.
 *
 * A BARE number stays hours. That is the existing meaning of this field, and
 * quietly reinterpreting it as minutes would shrink every saved plan by 60x.
 *
 * Returns null for anything unparseable, so a caller can leave the previous
 * value alone instead of coercing a typo to 0.
 */
export function parseDurationHours(text: string): number | null {
  if (typeof text !== 'string') return null;
  const trimmed = text.trim().toLowerCase();
  if (!trimmed) return null;
  const match = trimmed.match(/^([0-9]*\.?[0-9]+)\s*([a-z]*)$/);
  if (!match) return null;
  const value = parseFloat(match[1]);
  if (!Number.isFinite(value) || value < 0) return null;
  const unit = match[2];
  if (!unit || unit === 'h' || unit === 'hr' || unit === 'hrs'
      || unit === 'hour' || unit === 'hours') {
    return value;
  }
  if (unit === 'm' || unit === 'min' || unit === 'mins'
      || unit === 'minute' || unit === 'minutes') {
    return value / 60;
  }
  if (unit === 's' || unit === 'sec' || unit === 'secs'
      || unit === 'second' || unit === 'seconds') {
    return value / 3600;
  }
  return null;
}

/**
 * Hours rendered the way they were most likely entered ("20m", "1.5h").
 *
 * The trailing-zero strip is shared with :func:`splitDurationHours`, at the same
 * precision, so a plan labelled "1.6667h" opens as 1.6667 in the editor rather
 * than as one number in the label and another in the box. This used to read
 * `Number.isInteger(hours) ? \`${hours}h\` : \`${hours}h\`` -- a ternary whose two
 * branches were identical, so the guard it looked like it was applying was never
 * applied and a raw float went straight to the screen.
 */
export function formatDurationHours(hours: number): string {
  if (!Number.isFinite(hours) || hours <= 0) return '';
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  return `${parseFloat(hours.toFixed(4))}h`;
}

/** The unit a wall-time entry is expressed in. */
export type DurationUnit = 'm' | 'h';

/**
 * Hours split into the number and unit an editor should show.
 *
 * The plan stores hours, and a 10-minute cap stored as 0.16666666666666666 is
 * not a number anyone can read, edit, or retype -- which is exactly what the
 * approval card was asking for. Split it instead: 0.1666 -> ("10", "m").
 *
 * Splits on the same boundary as :func:`formatDurationHours` so the editor and
 * every label agree; a plan shown as "1.5h" opens as 1.5 with hours selected,
 * never as 90 minutes in one place and 1.5 hours in another.
 */
export function splitDurationHours(hours: number): { amount: string; unit: DurationUnit } {
  if (!Number.isFinite(hours) || hours <= 0) return { amount: '', unit: 'm' };
  if (hours < 1) return { amount: String(Math.round(hours * 60)), unit: 'm' };
  // Trailing zeros stripped: 2.50 reads as "2.5", 3.00 as "3". parseFloat of a
  // fixed string is the shortest way to do that without a formatting library.
  return { amount: String(parseFloat(hours.toFixed(4))), unit: 'h' };
}

/**
 * A number plus a chosen unit, in hours -- ``null`` when the number is unusable.
 *
 * The unit comes from a control, not from typing, so it is always one of two
 * known values; the amount still goes through :func:`parseDurationHours` so
 * there is one parser and one set of rules for what a valid entry is.
 */
export function durationToHours(amount: string, unit: DurationUnit): number | null {
  return parseDurationHours(`${String(amount).trim()}${unit}`);
}

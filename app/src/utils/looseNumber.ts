/**
 * The number in what a researcher typed into a numeric field, or null.
 *
 * Accepts a value with words around it -- "log S = −1.72", "0.75 log units",
 * "~ -393.5 kJ/mol" -- and the minus signs keyboards and copy-paste produce
 * (U+2212 minus, en and em dashes), which Number() rejects. The plan card's
 * target fields used Number() alone, so "log S = −1.72" became null and the
 * target was dropped without a word (run ec48cda0).
 *
 * Exactly one number must be present: "between 1 and 2" is ambiguous, so it is
 * null, and the caller says so instead of guessing.
 */
const NUMBER = /[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/g;

export function looseNumber(text: string): number | null {
  const normalized = text.replace(/[−‒–—﹣－]/g, '-').trim();
  if (!normalized) return null;
  const direct = Number(normalized);
  if (Number.isFinite(direct)) return direct;
  const found = normalized.match(NUMBER) ?? [];
  if (found.length !== 1) return null;
  const value = Number(found[0]);
  return Number.isFinite(value) ? value : null;
}

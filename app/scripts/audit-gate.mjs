#!/usr/bin/env node
// Dependency-vulnerability gate for the app's production dependency tree.
//
// `npm audit --audit-level=high` can't distinguish "a new advisory we must act
// on" from "an advisory with no fixed version, in build-only tooling, that was
// reviewed and accepted" -- so one unfixable advisory (braces, node-forge in the
// Expo/Metro toolchain) blocked every app deploy from Aug 7 to Oct 6. This gate
// fails on any high/critical advisory NOT in audit-allowlist.json, and on any
// allowlist entry past its review_by date, so an exception can't become
// permanent by being forgotten. Entries no longer reported are flagged for
// removal. No dependencies; Node >= 18.
//
//   node scripts/audit-gate.mjs            # from app/
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const SEVERE = new Set(['high', 'critical']);
const today = new Date().toISOString().slice(0, 10);

let raw;
try {
  raw = execFileSync('npm', ['audit', '--omit=dev', '--json'], {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
} catch (err) {
  // npm audit exits non-zero whenever it finds anything; the JSON is on stdout.
  raw = err.stdout;
  if (!raw) {
    console.error(`audit-gate: npm audit produced no report: ${err.message}`);
    process.exit(2);
  }
}
const report = JSON.parse(raw);
const allow = JSON.parse(readFileSync(new URL('../audit-allowlist.json', import.meta.url), 'utf8'));
const accepted = new Map(allow.accepted.map((e) => [e.id, e]));

// Root advisories only: npm repeats each one up every dependency chain
// (expo -> metro -> micromatch -> braces), which is noise for a gate.
const found = new Map();
for (const info of Object.values(report.vulnerabilities ?? {})) {
  for (const via of info.via) {
    if (typeof via === 'object' && SEVERE.has(via.severity)) {
      const id = via.url.split('/').pop();
      found.set(id, { id, package: via.name, severity: via.severity, title: via.title, url: via.url });
    }
  }
}

const unaccepted = [...found.values()].filter((a) => !accepted.has(a.id));
const expired = allow.accepted.filter((e) => found.has(e.id) && e.review_by < today);
const stale = allow.accepted.filter((e) => !found.has(e.id));

for (const a of found.values()) {
  const e = accepted.get(a.id);
  console.log(`${e ? 'accepted' : 'BLOCKING'}  ${a.severity.padEnd(8)} ${a.package.padEnd(28)} ${a.id}  ${a.title}`
    + (e ? `  (review by ${e.review_by})` : ''));
}
for (const e of stale) {
  console.log(`note      no longer reported: ${e.package} ${e.id} -- remove it from audit-allowlist.json`);
}
for (const e of expired) {
  console.error(`EXPIRED   ${e.package} ${e.id}: review_by ${e.review_by} has passed -- re-check for a fix, then renew or remove the entry`);
}
if (unaccepted.length || expired.length) {
  console.error(`audit-gate: FAIL -- ${unaccepted.length} unaccepted, ${expired.length} expired high/critical advisor(ies)`);
  process.exit(1);
}
console.log(`audit-gate: ok -- ${found.size} high/critical advisor(ies), all accepted and current`);

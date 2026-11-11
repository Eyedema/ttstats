// Condense a Playwright JSON report to counts plus one line per failure.
// Used by scripts/e2e_quiet.sh; see the comment there for why.
import { readFileSync } from 'node:fs';

const MAX_FAILURES = 25;
const report = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const stripAnsi = (s) => s.replace(/\x1b\[[0-9;]*m/g, '');

const failures = [];
const flaky = [];

function walk(suite, titles) {
  const here = suite.title && !suite.title.endsWith('.js') ? [...titles, suite.title] : titles;
  for (const spec of suite.specs ?? []) {
    for (const test of spec.tests ?? []) {
      const where = `[${test.projectName}] ${spec.file}:${spec.line} › ${[...here, spec.title].join(' › ')}`;
      if (test.status === 'unexpected') {
        const result = test.results.at(-1) ?? {};
        const err = result.error ?? result.errors?.[0] ?? {};
        const why = stripAnsi(err.message ?? result.status ?? 'unknown error')
          .split('\n')
          .map((l) => l.trim())
          .filter(Boolean)
          .slice(0, 2)
          .join(' | ');
        failures.push(`✘ ${where}\n    ${why.slice(0, 300)}`);
      } else if (test.status === 'flaky') {
        flaky.push(`~ ${where}`);
      }
    }
  }
  for (const child of suite.suites ?? []) walk(child, here);
}
for (const suite of report.suites ?? []) walk(suite, []);

const s = report.stats ?? {};
const secs = Math.round((s.duration ?? 0) / 1000);
console.log(
  `e2e: ${s.expected ?? 0} passed, ${s.unexpected ?? 0} failed, ` +
    `${s.flaky ?? 0} flaky, ${s.skipped ?? 0} skipped (${secs}s)`,
);
for (const e of report.errors ?? []) {
  console.log(`! ${stripAnsi(e.message ?? '').split('\n')[0].slice(0, 300)}`);
}
failures.slice(0, MAX_FAILURES).forEach((f) => console.log(f));
if (failures.length > MAX_FAILURES) console.log(`… and ${failures.length - MAX_FAILURES} more failures`);
flaky.forEach((f) => console.log(f));

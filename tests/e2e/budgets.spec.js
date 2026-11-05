const { test, expect } = require('@playwright/test');
const path = require('path');
const { resolvePages, bucketFor } = require('./perf-pages');

/**
 * Page-weight budgets (KAN-121 / S10.1 of the speed epic, KAN-119).
 *
 * Every key page, loaded cold (fresh context, empty cache), must stay under
 * its byte and request budget. Bytes are what crossed the wire: the
 * *compressed* body as WebKit reports it in `request.sizes()`, against the
 * prod-like static pipeline the e2e server runs (settings/e2e.py: hashed,
 * gzipped, served by WhiteNoise). Timing is not asserted here -- WebKit has
 * no network throttling and CI machines vary; `npm run perf` measures that.
 *
 * Values are the baseline in docs/decisions/kan-119-performance.md plus a
 * little headroom. When a story makes a page lighter, tighten its row here in
 * the same commit, so the win cannot quietly regress. Raising a budget needs
 * a reason in the commit message.
 *
 * Units: KB = 1024 bytes, compressed. `requests` counts every request the
 * page made up to network idle (service workers are blocked in this project).
 */
const BUDGETS = {
  // Baseline (WebKit, cold, KAN-121 @ 3437323) in the comment; budget = baseline
  // plus ~5% (JS/CSS: a few KB). Chart.js alone is ~69 KB compressed, so it
  // cannot land on a page budgeted without it.
  //                  js KB      css KB      total KB      requests
  dashboard:       { js: 40,  css: 10, total: 106, requests: 10 }, // 37.3 / 8.6 / 100.5 / 9
  play:            { js: 40,  css: 10, total: 106, requests: 10 }, // 37.3 / 8.6 / 100.1 / 9
  leaderboard:     { js: 40,  css: 10, total: 108, requests: 10 }, // 37.3 / 8.6 / 101.7 / 9
  'match list':    { js: 40,  css: 10, total: 108, requests: 10 }, // 37.3 / 8.6 / 102.5 / 9
  'match detail':  { js: 112, css: 10, total: 180, requests: 11 }, // 106.7 / 8.6 / 171.8 / 10
  'player detail': { js: 112, css: 10, total: 184, requests: 11 }, // 106.7 / 8.6 / 175.8 / 10
  'head-to-head':  { js: 112, css: 10, total: 182, requests: 11 }, // 106.7 / 8.6 / 174.1 / 10
};

/** Load `path` and total what crossed the wire, by type. */
async function weigh(page, path) {
  const pending = [];
  page.on('requestfinished', (request) => {
    pending.push(
      request.sizes().then((sizes) => ({
        url: request.url(),
        bucket: bucketFor(request.resourceType()),
        // Encoded (compressed) body plus headers: what the radio carried.
        bytes: sizes.responseBodySize + sizes.responseHeadersSize,
      })),
    );
  });
  const response = await page.goto(path, { waitUntil: 'load' });
  expect(response.status()).toBe(200);
  await page.waitForLoadState('networkidle');

  const entries = await Promise.all(pending);
  const kb = { js: 0, css: 0, total: 0 };
  for (const e of entries) {
    if (e.bucket === 'js') kb.js += e.bytes;
    if (e.bucket === 'css') kb.css += e.bytes;
    kb.total += e.bytes;
  }
  for (const k of Object.keys(kb)) kb[k] = Math.round((kb[k] / 1024) * 10) / 10;
  return { ...kb, requests: entries.length, entries };
}

// Not imported from auth.setup.js: requiring it would register its setup
// tests inside this file.
const AUTH_FILE = path.join(__dirname, '.auth', 'user.json');

let paths;

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ storageState: AUTH_FILE });
  const page = await context.newPage();
  paths = Object.fromEntries(
    (await resolvePages(page)).map((p) => [p.name, p.path]),
  );
  await context.close();
});

for (const [name, budget] of Object.entries(BUDGETS)) {
  test(`${name} stays within its page-weight budget`, async ({ page }, testInfo) => {
    const measured = await weigh(page, paths[name]);
    const summary = `${name}: js ${measured.js} KB, css ${measured.css} KB, ` +
      `total ${measured.total} KB, ${measured.requests} requests`;
    testInfo.annotations.push({ type: 'weight', description: summary });
    if (process.env.BUDGETS_VERBOSE) {
      console.log(summary);
      for (const e of measured.entries) console.log(`   ${e.bucket}\t${e.bytes}\t${e.url}`);
    }

    expect.soft(measured.js, `${summary} -- JS over budget`).toBeLessThanOrEqual(budget.js);
    expect.soft(measured.css, `${summary} -- CSS over budget`).toBeLessThanOrEqual(budget.css);
    expect.soft(measured.total, `${summary} -- total over budget`).toBeLessThanOrEqual(budget.total);
    expect.soft(measured.requests, `${summary} -- too many requests`).toBeLessThanOrEqual(budget.requests);
  });
}

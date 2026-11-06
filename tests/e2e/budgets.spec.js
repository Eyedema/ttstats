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
 * Brotli + gzip, served by WhiteNoise). Timing is not asserted here -- WebKit has
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
  // Measured (WebKit, cold, Brotli) after KAN-125 + KAN-126 in the comment. Budgets
  // are the KAN-122 rows (measured plus ~5%) raised by exactly what S10.6's
  // pull_to_refresh.js costs on every page: +3 KB js/total (2.5 KB on the
  // wire with headers), +1 request. Chart.js alone is ~60 KB compressed, so it
  // cannot land on a page budgeted without it. CSS was raised 8 -> 8.5 by
  // KAN-125: view transitions (+0.27 KB) left it at 7.97 KB, no headroom.
  // S10.1 baseline (gzip) is in
  // docs/decisions/kan-119-performance.md.
  //                  js KB      css KB     total KB      requests
  dashboard:       { js: 39,  css: 8.5, total: 104, requests: 11 }, // 36.1 / 8.0 / 98.9 / 10
  play:            { js: 39,  css: 8.5, total: 103, requests: 11 }, // 36.1 / 8.0 / 98.3 / 10
  leaderboard:     { js: 39,  css: 8.5, total: 105, requests: 11 }, // 36.1 / 8.0 / 100 / 10
  'match list':    { js: 39,  css: 8.5, total: 106, requests: 11 }, // 36.1 / 8.0 / 100.7 / 10
  'match detail':  { js: 102, css: 8.5, total: 169, requests: 12 }, // 96.2 / 8.0 / 160.9 / 11
  'player detail': { js: 102, css: 8.5, total: 173, requests: 12 }, // 96.2 / 8.0 / 164.9 / 11
  'head-to-head':  { js: 102, css: 8.5, total: 172, requests: 12 }, // 96.2 / 8.0 / 163.1 / 11
};

// WebKit, like Safari, only advertises Brotli over HTTPS. Prod is HTTPS, so
// an iPhone gets the `.br` files; the e2e server is plain HTTP, so without
// this WebKit asks for gzip and the budgets would weigh bytes no phone
// downloads. WebKit decodes `br` fine when the server sends it (KAN-122).
test.use({ extraHTTPHeaders: { 'Accept-Encoding': 'br, gzip, deflate' } });

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

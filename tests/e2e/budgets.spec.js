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
  // Measured (WebKit, cold, Brotli) after KAN-122 in the comment; budget =
  // measured plus ~5%, rounded up. Chart.js alone is ~60 KB compressed, so it
  // cannot land on a page budgeted without it. S10.1 baseline (gzip) is in
  // docs/decisions/kan-119-performance.md.
  //                  js KB      css KB     total KB      requests
  dashboard:       { js: 36,  css: 8, total: 101, requests: 10 }, // 33.7 / 7.5 / 95.7 / 9
  play:            { js: 36,  css: 8, total: 100, requests: 10 }, // 33.7 / 7.5 / 95.3 / 9
  leaderboard:     { js: 36,  css: 8, total: 102, requests: 10 }, // 33.7 / 7.5 / 96.8 / 9
  'match list':    { js: 36,  css: 8, total: 103, requests: 10 }, // 33.7 / 7.5 / 97.6 / 9
  'match detail':  { js: 99,  css: 8, total: 166, requests: 11 }, // 93.7 / 7.5 / 157.8 / 10
  'player detail': { js: 99,  css: 8, total: 170, requests: 11 }, // 93.7 / 7.5 / 161.7 / 10
  'head-to-head':  { js: 99,  css: 8, total: 169, requests: 11 }, // 93.7 / 7.5 / 160.1 / 10
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

const base = require('@playwright/test');
const { PROD_CSP } = require('./helpers');
const { startOfflineProxy } = require('./offline-proxy');

/**
 * KAN-136 (S12.5): after an online page load, when idle, the worker fetches
 * the core pages in the background, so they open offline without ever having
 * been visited. Never on a poor connection, never twice in 30 minutes.
 *
 * `iphone-pwa` (live worker), the network cut by offline-proxy.js, which also
 * counts the worker's warm-up requests (X-TTStats-Warm: 1) as they reach the
 * server: the worker's own fetches are invisible to page events. User
 * e2e-reader (flag on), empty cookie jar, a fresh login per test.
 */
const PASSWORD = 'e2e-local-only';

const test = base.test.extend({
  net: async ({ baseURL, context }, use) => {
    const net = await startOfflineProxy(baseURL, { csp: PROD_CSP });
    await use(net);
    await net.close();
  },
});
const { expect } = base;

test.use({ storageState: { cookies: [], origins: [] } });
// Fixed waits (idle periods) on top of a login: 60 s was tight on a loaded runner.
test.describe.configure({ timeout: 90000 });

async function login(page, net) {
  await page.goto(net.origin + '/accounts/login/');
  await page.fill('input[name="username"]', 'e2e-reader');
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([page.waitForURL(/\/pingpong\/$/), page.click('button[type="submit"]')]);
}

function storedPaths(page) {
  return page.evaluate(async () => {
    const paths = [];
    for (const name of await caches.keys()) {
      if (!name.startsWith('ttstats-snap-')) continue;
      for (const request of await (await caches.open(name)).keys()) {
        const url = new URL(request.url);
        paths.push(url.pathname + url.search);
      }
    }
    return paths;
  });
}

function warmList(page) {
  return page.evaluate(() => {
    const el = document.getElementById('snapshot-warm');
    return el ? JSON.parse(el.textContent) : null;
  });
}

test('fresh login online: the core pages are saved in the background and open offline', async ({ page, net }) => {
  await login(page, net);
  const urls = await warmList(page);
  // Today, the Table, the reader's own page, their confirmed match(es).
  expect(urls.slice(0, 2)).toEqual(['/pingpong/', '/pingpong/leaderboard/']);
  expect(urls[2]).toMatch(/^\/pingpong\/players\/\d+\/$/);
  const matches = urls.filter((u) => /^\/pingpong\/matches\/\d+\/$/.test(u));
  expect(matches.length).toBeGreaterThanOrEqual(1);
  expect(urls.length).toBeLessThanOrEqual(6);

  // Within one idle period, every one of them is stored (Today by the visit
  // itself or the warm-up, whichever came first).
  await expect.poll(() => storedPaths(page), { timeout: 20000 }).toEqual(expect.arrayContaining(urls));
  // At most one request per URL, and none for a page saved moments ago.
  expect(net.warmRequests.length).toBeLessThanOrEqual(urls.length);
  expect(new Set(net.warmRequests).size).toBe(net.warmRequests.length);
  for (const path of net.warmRequests) expect(urls).toContain(path);

  net.setMode('offline');
  // Never visited, yet there: the Table and a match, each labelled.
  for (const path of ['/pingpong/leaderboard/', matches[0]]) {
    await page.goto(net.origin + path);
    expect(await page.evaluate(() => document.documentElement.dataset.freshness)).toBe('snapshot');
    await expect(page.locator('[data-snapshot-banner]')).toBeVisible();
  }
  await page.goto(net.origin + '/pingpong/');
  expect(await page.evaluate(() => document.documentElement.dataset.freshness)).toBe('snapshot');
  // A saved copy never starts a warm-up of its own.
  const before = net.warmRequests.length;
  await page.waitForTimeout(3000);
  expect(net.warmRequests.length).toBe(before);
});

test('not twice within 30 minutes', async ({ page, net }) => {
  await login(page, net);
  const urls = await warmList(page);
  await expect.poll(() => storedPaths(page), { timeout: 20000 }).toEqual(expect.arrayContaining(urls));
  // Let the run finish (its last store lands before its reply would).
  await expect.poll(() => net.warmRequests.length, { timeout: 10000 }).toBeGreaterThan(0);
  await page.waitForTimeout(1000);
  const first = net.warmRequests.length;

  for (const path of ['/pingpong/leaderboard/', '/pingpong/']) {
    await page.goto(net.origin + path);
    await page.waitForLoadState('load');
  }
  // Two idle periods (the 2 s fallback where requestIdleCallback is missing).
  await page.waitForTimeout(5000);
  expect(net.warmRequests.length).toBe(first);
});

test('no warm-up while the connection is not online', async ({ page, net }) => {
  // connection.js's state, pinned to 'degraded' (it cannot overwrite it).
  await page.addInitScript(() => {
    Object.defineProperty(window, 'TTStatsConnection', {
      configurable: false,
      get: () => ({ state: () => 'degraded' }),
      set: () => {},
    });
  });
  await login(page, net);
  expect(await warmList(page)).not.toBeNull();
  expect(await page.evaluate(() => window.TTStatsConnection.state())).toBe('degraded');
  await page.waitForTimeout(5000);
  expect(net.warmRequests).toEqual([]);
  expect(await storedPaths(page)).not.toContain('/pingpong/leaderboard/');
});

const base = require('@playwright/test');
const { openMomentumMatch } = require('./helpers');
const { startOfflineProxy } = require('./offline-proxy');

/**
 * KAN-82: the live scoreboard opens with no signal; nothing else does.
 *
 * Runs in `iphone-pwa`, the one project where the service worker may control
 * the page. The network is cut by `offline-proxy.js`, never page.route() (a
 * controlling worker hides requests from it in WebKit,
 * docs/decisions/kan-76-sw-e2e-audit.md) and never context.setOffline()
 * (in WebKit it fails requests before the worker sees them, so nothing could
 * ever be served offline; see offline-proxy.js).
 *
 * Every test gets its own proxy origin, so the worker and its caches start
 * empty. It shares the seeded live match with scoreboard.spec.js, so it never
 * assumes a score: it reads the score online and requires the same offline.
 *
 * The selectors below are the scoreboard's; if its markup changes, change
 * them here (and only here).
 */
const team1Score = (page) => page.locator('[x-ref="team1Score"]');
const team2Score = (page) => page.locator('[x-ref="team2Score"]');
const tapZones = (page) => page.locator('.tap-zone');

const test = base.test.extend({
  // Not `proxy`: that name is Playwright's own context option. Depends on
  // `context` so it is torn down FIRST: a request the proxy is holding
  // (`hang`) keeps the worker's fetch event alive, and WebKit will not close
  // a context whose worker is still busy -- the run then hangs forever.
  net: async ({ baseURL, context }, use) => {
    const net = await startOfflineProxy(baseURL);
    await use(net);
    await net.close();
  },
});
const { expect } = base;

async function waitForControl(page) {
  await expect
    .poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller)), {
      timeout: 10000,
    })
    .toBe(true);
}

/** Through the proxy: Today, until the worker controls it; returns the seeded
 *  live match's scoreboard path, read off the Resume link. */
async function scoreboardPath(page, net) {
  await page.goto(net.origin + '/pingpong/');
  await waitForControl(page);
  const href = await page.getByRole('link', { name: /Resume/i }).first().getAttribute('href');
  expect(href).toMatch(/^\/pingpong\/matches\/\d+\/live\/$/);
  return href;
}

/** When the stored copy of `path` was stored (ms), or 0 if there is none. */
function storedAt(page, path) {
  return page.evaluate(async (p) => {
    for (const name of await caches.keys()) {
      if (!name.startsWith('ttstats-live-')) continue;
      const hit = await (await caches.open(name)).match(location.origin + p);
      if (hit) return Number(hit.headers.get('X-TTStats-Stored-At')) || 1;
    }
    return 0;
  }, path);
}

function liveCacheNames(page) {
  return page.evaluate(async () =>
    (await caches.keys()).filter((name) => name.startsWith('ttstats-live-'))
  );
}

/** Open the scoreboard online, past the server picker, and let the worker
 *  store the started page. Returns the score as shown. */
async function openStartedScoreboard(page, net, path) {
  await page.goto(net.origin + path);
  const picker = page.getByRole('heading', { name: /serving/i });
  await expect(picker.or(team1Score(page)).first()).toBeVisible();
  if (await picker.isVisible()) {
    await page.getByRole('button', { name: 'E2E Player' }).tap();
    await expect(team1Score(page)).toBeVisible();
  }
  // Load once more so the stored copy is of a started match.
  const before = Date.now();
  await page.reload();
  await expect(team1Score(page)).toBeVisible();
  await expect.poll(() => storedAt(page, path), { timeout: 10000 }).toBeGreaterThanOrEqual(before);
  return [await team1Score(page).innerText(), await team2Score(page).innerText()];
}

async function expectScoreboard(page, [one, two]) {
  await expect(team1Score(page)).toHaveText(one);
  await expect(team2Score(page)).toHaveText(two);
  await expect(tapZones(page)).toHaveCount(2);
  await expect(tapZones(page).first()).toBeVisible();
}

test.describe('offline scoreboard', () => {
  test('reloads with no signal, with the score and tap zones', async ({ page, net }) => {
    const path = await scoreboardPath(page, net);
    const score = await openStartedScoreboard(page, net, path);

    net.setMode('offline');
    const response = await page.reload();
    expect(response.ok()).toBe(true);
    await expectScoreboard(page, score);

    // Its stylesheet came out of the static cache, not the network.
    const styled = await page.evaluate(() =>
      [...document.styleSheets].some(
        (sheet) =>
          sheet.href && sheet.href.includes('/static/pingpong/css/app.') && sheet.cssRules.length > 0
      )
    );
    expect(styled).toBe(true);

    // JSON is never answered by the worker: offline, it fails honestly.
    const state = await page.evaluate(
      (p) => fetch(p + 'state/', { credentials: 'same-origin' }).then((r) => r.status, () => 'network error'),
      path
    );
    expect(state).toBe('network error');
  });

  test('a network that never answers gives up and serves the copy', async ({ page, net }) => {
    const path = await scoreboardPath(page, net);
    const score = await openStartedScoreboard(page, net, path);

    net.setMode('hang');
    const started = Date.now();
    try {
      // Not "load": requests the worker does not answer (the manifest) hang
      // too, as they would on lie-fi, and hold the load event. The page is
      // usable long before.
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expectScoreboard(page, score);
      // NAV_TIMEOUT_MS is 3000.
      expect(Date.now() - started).toBeLessThan(6000);
    } finally {
      // Release the held requests now: WebKit will not tear down a page
      // with loads still in flight.
      net.setMode('online');
    }
  });

  test('back online, a tap reaches the server', async ({ page, net }) => {
    const path = await scoreboardPath(page, net);
    const score = await openStartedScoreboard(page, net, path);

    net.setMode('offline');
    await page.reload();
    await expectScoreboard(page, score);
    net.setMode('online');
    await page.reload();
    await expectScoreboard(page, score);

    const read = () =>
      page.evaluate((p) => fetch(p + 'state/', { credentials: 'same-origin' }).then((r) => r.json()), path);
    const before = JSON.stringify((await read()).state);

    await tapZones(page).first().tap();

    await expect
      .poll(async () => JSON.stringify((await read()).state), { timeout: 10000 })
      .not.toBe(before);
  });

  test('offline, any other page is the browser error, never a cached copy', async ({ page, net }) => {
    const path = await scoreboardPath(page, net);
    await openStartedScoreboard(page, net, path);
    await page.goto(net.origin + '/pingpong/leaderboard/');
    await waitForControl(page);

    net.setMode('offline');
    await expect(page.goto(net.origin + '/pingpong/leaderboard/')).rejects.toThrow();
    await expect(page.goto(net.origin + '/pingpong/')).rejects.toThrow();
    // The same network, and the scoreboard still opens: the errors above are
    // the worker declining, not a test that could never have succeeded.
    await page.goto(net.origin + path);
    await expect(team1Score(page)).toBeVisible();
  });
});

test.describe('evicting stored scoreboards', () => {
  test('live-match-complete drops that match', async ({ page, net }) => {
    const path = await scoreboardPath(page, net);
    await openStartedScoreboard(page, net, path);

    await page.evaluate((p) => {
      navigator.serviceWorker.controller.postMessage({ type: 'live-match-complete', url: p });
    }, path);

    await expect.poll(() => storedAt(page, path)).toBe(0);
  });

  test('a scoreboard that now redirects (match over) is dropped', async ({ page, net }) => {
    const finished = (await openMomentumMatch(page)) + 'live/';
    const path = await scoreboardPath(page, net);
    await openStartedScoreboard(page, net, path);

    // Plant a copy for the seeded finished match's scoreboard, then open it
    // online: the server redirects to match detail and the copy must go.
    await page.evaluate(async (p) => {
      const name = (await caches.keys()).find((n) => n.startsWith('ttstats-live-'));
      const headers = { 'Content-Type': 'text/html', 'X-TTStats-Stored-At': String(Date.now()) };
      await (await caches.open(name)).put(location.origin + p, new Response('stale', { headers }));
    }, finished);
    expect(await storedAt(page, finished)).toBeGreaterThan(0);

    await page.goto(net.origin + finished);
    expect(new URL(page.url()).pathname).not.toBe(finished);
    await expect.poll(() => storedAt(page, finished)).toBe(0);
    // The live match's own copy is untouched.
    expect(await storedAt(page, path)).toBeGreaterThan(0);
  });

  test('Log out purges every stored scoreboard before the POST leaves', async ({ page, net }) => {
    const path = await scoreboardPath(page, net);
    await openStartedScoreboard(page, net, path);
    expect(await liveCacheNames(page)).toHaveLength(1);

    // Submit the real logout form, but cancel the navigation: logging out
    // would end the session every other spec shares.
    await page.evaluate(() => {
      const form = document.querySelector('form[data-sw-purge]');
      form.addEventListener('submit', (event) => event.preventDefault());
      form.requestSubmit();
    });

    await expect.poll(() => liveCacheNames(page)).toHaveLength(0);
  });

  test("a page announcing another identity drops this one's copies", async ({ page, net }) => {
    const path = await scoreboardPath(page, net);
    await openStartedScoreboard(page, net, path);

    // What the next person's first page does (offline.js, on load).
    await page.evaluate(() => {
      navigator.serviceWorker.controller.postMessage({ type: 'identity', identity: '0123456789abcdef' });
    });
    await expect.poll(() => liveCacheNames(page)).toHaveLength(0);

    // This user's next page load does not resurrect anything...
    await page.goto(net.origin + '/pingpong/');
    expect(await liveCacheNames(page)).toHaveLength(0);
    // ...until the scoreboard is opened online again.
    await page.goto(net.origin + path);
    await expect.poll(() => storedAt(page, path), { timeout: 10000 }).toBeGreaterThan(0);
  });
});

test("a new build deletes the previous build's caches", async ({ page, net }) => {
  await scoreboardPath(page, net);
  await page.evaluate(async () => {
    await caches.open('ttstats-static-ttstats-v2-0000000000');
    await caches.open('ttstats-live-ttstats-v2-0000000000-0123456789abcdef');
    await caches.open('not-ours');
    // Reinstall the worker: activate runs again, as after a deploy.
    const registration = await navigator.serviceWorker.getRegistration('/');
    await registration.unregister();
  });

  await page.reload();
  await expect
    .poll(() => page.evaluate(() => caches.keys()), { timeout: 10000 })
    .not.toContain('ttstats-static-ttstats-v2-0000000000');
  const names = await page.evaluate(() => caches.keys());
  expect(names).not.toContain('ttstats-live-ttstats-v2-0000000000-0123456789abcdef');
  expect(names).toContain('not-ours');
  expect(names.some((name) => /^ttstats-static-ttstats-v3-[0-9a-f]{10}$/.test(name))).toBe(true);
});

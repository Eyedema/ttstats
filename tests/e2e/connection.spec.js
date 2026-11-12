const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP, createLiveMatch, finishLiveMatch } = require('./helpers');
const { watchPrefetches, tab } = require('./prefetch-helpers');

/**
 * Poor-signal detection for in-page requests (KAN-135 / S12.4), iphone-safari
 * (service workers blocked, so page.route sees every request; this story does
 * not need the worker).
 *
 * connection.js keeps html[data-connection] = online | degraded | offline
 * from what htmx requests actually did, gives htmx a timeout, and turns a
 * failed swap into an honest toast with the trigger left usable. The live
 * scoreboard is exempt: KAN-76 owns its sync and its own error UI.
 *
 * The leaderboard's Season select is an htmx GET form (hx-trigger="change,
 * submit"), as the `e2e-seasons` user who has season data.
 */
test.use({ storageState: path.join(__dirname, '.auth', 'seasons.json') });

const FRAGMENT = /\/pingpong\/leaderboard\/\?/;
const BALL = 'rgb(245, 158, 11)';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const connection = (page) => page.evaluate(() => document.documentElement.getAttribute('data-connection'));
const toast = (page) => page.locator('[data-toast]');

/** Route the leaderboard fragment through `handler` (after the CSP route, so it wins). */
async function routeFragment(page, handler) {
  await page.route(FRAGMENT, async (route) => {
    try {
      await handler(route);
    } catch (err) {
      if (!/disposed|Test ended|closed|already handled/i.test(String(err))) throw err;
    }
  });
}

async function openLeaderboard(page) {
  await page.goto('/pingpong/leaderboard/');
  await page.waitForLoadState('load');
  await expect.poll(() => connection(page)).toBe('online');
}

/** Pick a season the select is not on, so the change always fires. */
async function changeSeason(page) {
  const select = page.locator('#id_season');
  const current = await select.inputValue();
  const values = await select.locator('option').evaluateAll((os) => os.map((o) => o.value));
  const next = values.find((v) => v !== current);
  await select.selectOption(next);
  return next;
}

test.describe('connection state and toasts', () => {
  let errors;

  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    page.on('console', (m) => {
      if (/Content Security Policy|Refused to/i.test(m.text())) errors.push(`csp: ${m.text()}`);
    });
    await applyProdCSP(page);
  });

  test.afterEach(() => {
    expect(errors).toEqual([]);
  });

  test('a filter with the network gone: toast, controls usable, and Try again recovers', async ({ page }) => {
    await openLeaderboard(page);
    expect(await page.evaluate(() => window.htmx.config.timeout)).toBe(8000);

    await routeFragment(page, (route) => route.abort('internetdisconnected'));
    await changeSeason(page);

    // Within htmx.config.timeout (8 s), not after it: a send error is immediate.
    await expect(toast(page)).toHaveText(/No connection — try again/, { timeout: 8000 });
    await expect(page.locator('#ttstats-toasts')).toHaveAttribute('role', 'status');
    await expect(page.locator('#ttstats-toasts')).toHaveAttribute('aria-live', 'polite');
    expect(await connection(page)).toBe('degraded');

    // Nothing left spinning or disabled; the select still works.
    await expect(page.locator('.htmx-request')).toHaveCount(0);
    await expect(page.locator('[data-disabled-by-htmx], #id_season[disabled]')).toHaveCount(0);
    await expect(page.locator('#id_season')).toBeEnabled();

    // Not amber, not red: a neutral surface.
    const colours = await toast(page).evaluate((el) => {
      const s = getComputedStyle(el);
      return [s.backgroundColor, s.borderTopColor, s.borderLeftColor, s.color];
    });
    for (const c of colours) expect(c).not.toBe(BALL);
    expect(colours[0]).not.toMatch(/^rgb\(2[0-9]{2}, [0-9]{1,2}, [0-9]{1,2}\)$/); // no red fill

    // Above the tab bar, never over it.
    const toastBox = await toast(page).boundingBox();
    const barBox = await page.locator('nav[aria-label="Primary"]').boundingBox();
    expect(toastBox.y + toastBox.height).toBeLessThanOrEqual(barBox.y);

    // A second network error in a row: offline.
    await changeSeason(page);
    await expect.poll(() => connection(page)).toBe('offline');

    // The network comes back; Try again repeats the GET, no auto-retry before.
    await page.unroute(FRAGMENT);
    const responded = page.waitForResponse((r) => FRAGMENT.test(r.url()) && r.status() === 200);
    await toast(page).getByRole('button', { name: 'Try again' }).tap();
    await responded;
    await expect(toast(page)).toHaveCount(0);
    await expect.poll(() => connection(page)).toBe('online');
  });

  test('slow requests mark the link degraded; a fast one brings it back', async ({ page }) => {
    await openLeaderboard(page);
    let slow = true;
    // 5 s, not 3: the in-flight re-check fires at SLOW_MS + slack (2.55 s) and
    // WebKit's timers run ~100 ms late under load, so a 3 s answer left too
    // narrow a window to prove "degraded before it lands" (flaky in CI).
    await routeFragment(page, async (route) => {
      if (slow) await sleep(5000);
      await route.fallback();
    });

    let responded = page.waitForResponse((r) => FRAGMENT.test(r.url()));
    await changeSeason(page);
    // Degraded while still in flight (SLOW_MS = 2.5 s), before it even lands.
    await expect.poll(() => connection(page), { timeout: 4500 }).toBe('degraded');
    await responded;
    expect(await connection(page)).toBe('degraded');
    await expect(toast(page)).toHaveCount(0); // slow but answered: no error to report

    slow = false;
    responded = page.waitForResponse((r) => FRAGMENT.test(r.url()));
    await changeSeason(page);
    await responded;
    await expect.poll(() => connection(page)).toBe('online');
  });

  test('the connection event fires on every change', async ({ page }) => {
    await openLeaderboard(page);
    await page.evaluate(() => {
      window.__seen = [];
      document.addEventListener('ttstats:connection', (e) => window.__seen.push([e.detail.previous, e.detail.state]));
    });
    await routeFragment(page, (route) => route.abort('internetdisconnected'));
    await changeSeason(page);
    await expect.poll(() => page.evaluate(() => window.__seen)).toEqual([['online', 'degraded']]);
    expect(await page.evaluate(() => window.TTStatsConnection.state())).toBe('degraded');
  });

  test('a request that hangs past the timeout says the connection is very slow', async ({ page }) => {
    await openLeaderboard(page);
    // The real value is 8 s (asserted above); shortened so the spec is quick.
    await page.evaluate(() => { window.htmx.config.timeout = 1200; });
    // Playwright-WebKit quirk: while a route holds a request, the XHR's own
    // timer cannot fire; it reports the timeout when the route lets go
    // (measured: timeout 1 s, route held 4 s -> ontimeout at 4 s). So the
    // route holds past the timeout and then lets go.
    await routeFragment(page, async (route) => {
      await sleep(2500);
      await route.abort('timedout');
    });
    await changeSeason(page);
    await expect(toast(page)).toHaveText(/Connection is very slow — try again/, { timeout: 6000 });
    await expect(toast(page).getByRole('button', { name: 'Try again' })).toBeVisible();
    await expect(page.locator('.htmx-request')).toHaveCount(0);
    expect(await connection(page)).toBe('degraded');
  });

  test('a 5xx gets a server-error toast, a 4xx none', async ({ page }) => {
    await openLeaderboard(page);
    let status = 502;
    await routeFragment(page, (route) => route.fulfill({ status, body: 'bad gateway' }));
    await changeSeason(page);
    await expect(toast(page)).toHaveText(/went wrong on our side — try again/);
    // The server answered quickly: the link itself is fine.
    expect(await connection(page)).toBe('online');

    await toast(page).getByRole('button', { name: 'Dismiss' }).tap();
    await expect(toast(page)).toHaveCount(0);
    status = 404;
    const responded = page.waitForResponse((r) => FRAGMENT.test(r.url()));
    await changeSeason(page);
    await responded;
    await page.waitForTimeout(300);
    await expect(toast(page)).toHaveCount(0);
  });

  test('the toast dismisses itself', async ({ page }) => {
    await openLeaderboard(page);
    await page.evaluate(() => window.TTStatsToast.show('Needs a connection', { icon: 'wifi-off', duration: 500 }));
    await expect(toast(page)).toHaveText(/Needs a connection/);
    await expect(toast(page)).toHaveCount(0, { timeout: 2000 });
  });
});

test.describe('prefetch on a poor link', () => {
  test.beforeEach(async ({ page }) => { await applyProdCSP(page); });

  test('issues no request while degraded or offline, and resumes once online', async ({ page }) => {
    await openLeaderboard(page);
    const prefetches = watchPrefetches(page);

    await routeFragment(page, (route) => route.abort('internetdisconnected'));
    await changeSeason(page);
    await expect.poll(() => connection(page)).toBe('degraded');
    await tab(page, '/pingpong/').dispatchEvent('touchstart');
    await page.waitForTimeout(400);
    expect(prefetches).toHaveLength(0);

    await changeSeason(page);
    await expect.poll(() => connection(page)).toBe('offline');
    await tab(page, '/pingpong/championships/').dispatchEvent('touchstart');
    await page.waitForTimeout(400);
    expect(prefetches).toHaveLength(0);

    // Positive control: the same gesture prefetches once the link recovers.
    await page.unroute(FRAGMENT);
    const responded = page.waitForResponse((r) => FRAGMENT.test(r.url()) && r.status() === 200);
    await changeSeason(page);
    await responded;
    await expect.poll(() => connection(page)).toBe('online');
    await tab(page, '/pingpong/').dispatchEvent('touchstart');
    await expect.poll(() => prefetches.length).toBe(1);
  });
});

test.describe('the "Slow connection" line', () => {
  /** Serve the leaderboard page with KAN-132's snapshots meta switched on. */
  async function withSnapshotsMeta(page) {
    await page.route(/\/pingpong\/leaderboard\/$/, async (route) => {
      if (route.request().resourceType() !== 'document') return route.fallback();
      const response = await route.fetch();
      const body = (await response.text()).replace(
        '<meta name="ttstats-identity"',
        '<meta name="ttstats-snapshots" content="on"><meta name="ttstats-identity"',
      );
      await route.fulfill({ response, body });
    });
  }

  test.beforeEach(async ({ page }) => { await applyProdCSP(page); });

  test('shows only while degraded, and only with offline snapshots on', async ({ page }) => {
    await withSnapshotsMeta(page);
    await openLeaderboard(page);
    const hint = page.locator('#connection-hint');
    await expect(hint).toBeHidden();

    let slow = true;
    await routeFragment(page, async (route) => {
      if (slow) await sleep(3000);
      await route.fallback();
    });
    let responded = page.waitForResponse((r) => FRAGMENT.test(r.url()));
    await changeSeason(page);
    await responded;
    await expect(hint).toBeVisible();
    await expect(hint).toHaveText('Slow connection');
    expect(await hint.evaluate((el) => getComputedStyle(el).color)).not.toBe(BALL);
    expect(await hint.evaluate((el) => getComputedStyle(el).pointerEvents)).toBe('none');

    slow = false;
    responded = page.waitForResponse((r) => FRAGMENT.test(r.url()));
    await changeSeason(page);
    await responded;
    await expect(hint).toBeHidden();
  });

  test('never appears without the snapshots meta', async ({ page }) => {
    await openLeaderboard(page);
    await routeFragment(page, async (route) => { await sleep(3000); await route.fallback(); });
    const responded = page.waitForResponse((r) => FRAGMENT.test(r.url()));
    await changeSeason(page);
    await responded;
    expect(await connection(page)).toBe('degraded');
    await expect(page.locator('#connection-hint')).toHaveCount(0);
  });
});

test.describe('the live scoreboard is exempt', () => {
  test.use({ storageState: path.join(__dirname, '.auth', 'user.json') });

  test('no htmx timeout, no toasts, whatever the network does', async ({ page }) => {
    await applyProdCSP(page);
    const live = await createLiveMatch(page, { opponent: 'E2E Chaos' });
    try {
      await page.goto(live.url);
      await page.waitForLoadState('load');
      await expect(page.locator('#scoreboard')).toBeAttached();

      const info = await page.evaluate(() => ({
        exempt: window.TTStatsConnection.exempt,
        timeout: window.htmx ? window.htmx.config.timeout : 0,
      }));
      expect(info).toEqual({ exempt: true, timeout: 0 });

      // What would raise a toast anywhere else.
      await page.evaluate(() => {
        const xhr = { status: 0 };
        const detail = { xhr, elt: document.body, requestConfig: { verb: 'get' }, pathInfo: { requestPath: '/x/' } };
        for (const name of ['htmx:beforeRequest', 'htmx:sendError', 'htmx:timeout']) {
          document.body.dispatchEvent(new CustomEvent(name, { bubbles: true, detail }));
        }
        document.body.dispatchEvent(new CustomEvent('htmx:responseError', {
          bubbles: true, detail: { ...detail, xhr: { status: 502 } },
        }));
      });
      // And the real thing: the network goes away under the scoreboard.
      await page.context().setOffline(true);
      await page.waitForTimeout(500);
      await page.context().setOffline(false);
      await page.waitForTimeout(300);

      await expect(page.locator('[data-toast]')).toHaveCount(0);
      await expect(page.locator('#ttstats-toasts')).toHaveCount(0);
      await expect(page.locator('#connection-hint')).toHaveCount(0);
    } finally {
      await page.context().setOffline(false);
      await finishLiveMatch(page, live.pk);
    }
  });
});

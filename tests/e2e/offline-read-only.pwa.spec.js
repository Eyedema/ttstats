const base = require('@playwright/test');
const { PROD_CSP } = require('./helpers');
const { startOfflineProxy } = require('./offline-proxy');

/**
 * Read-only mode on a saved copy (KAN-146; KAN-138 cases 10, 11 and the
 * second half of 5).
 *
 * The worker serves a saved copy when the network is gone; on it nothing may
 * reach the server by accident (forms, htmx, links to pages that cannot open
 * offline say "Needs a connection"), no amber may claim something is live,
 * and once the network is back the banner offers a Refresh, never reloading
 * by itself.
 *
 * `iphone-pwa` with the network cut by offline-proxy.js, as in
 * offline-snapshots.pwa.spec.js (read its header: never setOffline(), never
 * page.route() under a live worker). The proxy adds the production CSP and
 * logs every request that reaches it, which is what "no request went out" is
 * asserted against. Users from seed_e2e: e2e-reader (A, in two clubs) and
 * e2e-reader-b (B, scoring a live match). Both have the snapshot flag on.
 */
const PASSWORD = 'e2e-local-only';
const READER = 'e2e-reader';
const READER_B = 'e2e-reader-b';
const AMBER = '245, 158, 11'; // --ball in app.css

const test = base.test.extend({
  // Torn down after `context` (see offline-snapshots.pwa.spec.js).
  net: async ({ baseURL, context }, use) => {
    const net = await startOfflineProxy(baseURL, { csp: PROD_CSP });
    await use(net);
    await net.close();
  },
});
const { expect } = base;

test.use({ storageState: { cookies: [], origins: [] } });
test.describe.configure({ timeout: 60000 });

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', (event) => {
      if (event.blockedURI !== 'eval') {
        window.__cspViolations.push(`${event.violatedDirective} ${event.blockedURI}`);
      }
    });
  });
});

test.afterEach(async ({ page }) => {
  // Under the production CSP, nothing this story added may be refused.
  const violations = await page.evaluate(() => window.__cspViolations || []).catch(() => []);
  expect(violations).toEqual([]);
});

async function login(page, net, username) {
  await page.goto(net.origin + '/accounts/login/');
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([page.waitForURL(/\/pingpong\//), page.click('button[type="submit"]')]);
  await expect
    .poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller)), { timeout: 10000 })
    .toBe(true);
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

/** Open `path` online until the worker has stored it. */
async function visit(page, net, path) {
  await expect(async () => {
    await page.goto(net.origin + path);
    await expect.poll(() => storedPaths(page), { timeout: 3000 }).toContain(path);
  }).toPass({ timeout: 20000 });
}

/** Cut the network and open `path` from its saved copy. */
async function openSaved(page, net, path) {
  net.setMode('offline');
  await page.goto(net.origin + path);
  await expect(page.locator('html')).toHaveAttribute('data-freshness', 'snapshot');
  await expect(banner(page)).toBeVisible();
}

const banner = (page) => page.locator('[data-snapshot-banner]');
const toast = (page) => page.locator('[data-toast]');
const refresh = (page) => banner(page).getByRole('button', { name: 'Refresh' });

/** Requests that reached the proxy since `mark` (an index into net.requests). */
function sentSince(net, mark, predicate) {
  return net.requests.slice(mark).filter(predicate);
}

/** Every element whose computed colours include the amber token's. */
function amberElements(page) {
  return page.evaluate((amber) => {
    const props = [
      'color', 'backgroundColor', 'borderTopColor', 'borderRightColor', 'borderBottomColor',
      'borderLeftColor', 'outlineColor', 'fill', 'stroke', 'textDecorationColor', 'boxShadow',
    ];
    const found = [];
    for (const el of document.querySelectorAll('*')) {
      const style = getComputedStyle(el);
      if (style.display === 'none') continue;
      for (const prop of props) {
        if (String(style[prop]).includes(amber)) {
          found.push(`${el.tagName.toLowerCase()}[class="${el.getAttribute('class') || ''}"] ${prop}`);
        }
      }
    }
    return found;
  }, AMBER);
}

test.describe('read-only mode on a saved copy', () => {
  test('submitting a form says "Needs a connection" and sends nothing', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/');
    await visit(page, net, '/pingpong/leaderboard/');

    await openSaved(page, net, '/pingpong/');
    const mark = net.requests.length;

    // A plain POST form: the drawer's club switcher (A is in two clubs).
    await page.locator('#mobile-menu-open').click();
    const other = page
      .locator('#mobile-menu-panel [data-club-switcher] button[type="submit"]:not([aria-current])')
      .first();
    await other.click();
    await expect(toast(page)).toContainText('Needs a connection');
    await expect(page.locator('html')).toHaveAttribute('data-freshness', 'snapshot');
    await page.keyboard.press('Escape');

    // An htmx form: the Table's season filter fires on change.
    await openSaved(page, net, '/pingpong/leaderboard/');
    const mark2 = net.requests.length;
    const select = page.locator('#season-filter-form select').first();
    const values = await select.locator('option').evaluateAll((os) => os.map((o) => o.value));
    await select.selectOption(values[values.length - 1]);
    await expect(toast(page)).toContainText('Needs a connection');
    // htmx never sent it: nothing is in flight and connection.js saw nothing.
    await expect(page.locator('.htmx-request')).toHaveCount(0);

    await page.waitForTimeout(500);
    expect(sentSince(net, mark, (r) => r.method === 'POST')).toEqual([]);
    expect(sentSince(net, mark2, (r) => r.path.startsWith('/pingpong/leaderboard/'))).toEqual([]);
  });

  test('a link to a page that cannot open offline says so; a saved one opens', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/leaderboard/');
    await visit(page, net, '/pingpong/');
    const play = await page
      .getByRole('navigation', { name: 'Primary' })
      .getByRole('link', { name: 'Play' })
      .getAttribute('href');

    await openSaved(page, net, '/pingpong/');
    const mark = net.requests.length;
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Play' }).click();
    await expect(toast(page)).toContainText('Needs a connection');
    await page.waitForTimeout(500);
    expect(new URL(page.url()).pathname).toBe('/pingpong/');
    expect(sentSince(net, mark, (r) => r.path.startsWith(play))).toEqual([]);

    // The Table is saved: the worker tries the network, then serves its copy.
    await Promise.all([
      page.waitForURL(/\/pingpong\/leaderboard\/$/),
      page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Table' }).click(),
    ]);
    await expect(page.locator('html')).toHaveAttribute('data-freshness', 'snapshot');
    await expect(banner(page)).toBeVisible();
  });

  test('logging out offline: the session needs the server, the saved pages go anyway', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/');
    await openSaved(page, net, '/pingpong/');
    const mark = net.requests.length;

    await page.evaluate(() => document.querySelector('form[data-sw-purge]').requestSubmit());
    await expect(toast(page)).toContainText('Needs a connection to log out');
    expect(new URL(page.url()).pathname).toBe('/pingpong/');
    expect(sentSince(net, mark, (r) => r.method === 'POST')).toEqual([]);
    // offline.js still told the worker to purge: nothing of A's is left.
    await expect.poll(() => storedPaths(page)).toEqual([]);
  });

  test('the saved time also reads as an age, refreshed in place', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/');
    await openSaved(page, net, '/pingpong/');
    const text = await banner(page).innerText();
    expect(text).toMatch(/^No connection — saved copy from \d\d:\d\d \((just now|\d+ min ago)\)$/);
    // Still the absolute time in <time>; the age sits beside it.
    await expect(page.locator('time[data-snapshot-time]')).toHaveText(/^\d\d:\d\d$/);
    await expect(page.locator('[data-snapshot-relative]')).toHaveCount(1);
  });
});

test.describe('no amber on a saved copy (KAN-138 case 11)', () => {
  test('Today with a live match: amber online, none on the copy', async ({ page, net }) => {
    await login(page, net, READER_B);
    await visit(page, net, '/pingpong/');
    // The pairing: the live block and the header dot ARE amber online, so
    // the scan below can see amber when it is there.
    await expect(page.getByTestId('live-resume-banner')).toBeVisible();
    expect((await amberElements(page)).length).toBeGreaterThan(0);

    await openSaved(page, net, '/pingpong/');
    await expect(page.getByTestId('live-resume-banner')).toBeVisible();
    await expect(page.locator('.label-live').first()).toBeVisible();
    expect(await amberElements(page)).toEqual([]);
    // The pulse is a live signal too.
    const animations = await page
      .locator('.live-dot')
      .evaluateAll((dots) => dots.map((d) => getComputedStyle(d).animationName));
    expect(animations.length).toBeGreaterThan(0);
    for (const name of animations) expect(name).toBe('none');
  });
});

test.describe("You're back online · Refresh", () => {
  test('lie-fi recovers: the late answer offers a Refresh, which loads the live page', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/leaderboard/');

    net.setMode('hang');
    try {
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('html')).toHaveAttribute('data-freshness', 'snapshot');
      await expect(refresh(page)).toHaveCount(0);

      net.release();
      await expect(banner(page)).toContainText('You’re back online', { timeout: 10000 });
      await expect(refresh(page)).toBeVisible();
      await expect(banner(page)).not.toContainText('No connection', { useInnerText: true });
      // Never by itself: still the saved copy.
      await page.waitForTimeout(1000);
      await expect(page.locator('html')).toHaveAttribute('data-freshness', 'snapshot');

      await Promise.all([page.waitForEvent('load'), refresh(page).click()]);
      await expect(page.locator('html')).toHaveAttribute('data-freshness', 'network');
      await expect(banner(page)).toBeHidden();
    } finally {
      net.setMode('online');
    }
  });

  test('a dead network comes back: the online event and a probe offer the Refresh', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/');
    await openSaved(page, net, '/pingpong/');

    // Still dead: the event alone proves nothing, the probe fails.
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await page.waitForTimeout(1000);
    await expect(refresh(page)).toHaveCount(0);

    net.setMode('online');
    const mark = net.requests.length;
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect(refresh(page)).toBeVisible({ timeout: 10000 });
    // The probe: a HEAD of the manifest, which the worker does not answer.
    expect(sentSince(net, mark, (r) => r.method === 'HEAD').map((r) => r.path)).toContain(
      '/manifest.webmanifest'
    );
    await expect(page.locator('html')).toHaveAttribute('data-freshness', 'snapshot');

    // Back online, an action asks for the refresh rather than "no connection".
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Play' }).click();
    await expect(toast(page)).toContainText('Refresh to get the live page first');
  });
});

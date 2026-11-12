const base = require('@playwright/test');
const { PROD_CSP } = require('./helpers');
const { startOfflineProxy } = require('./offline-proxy');

/**
 * KAN-137 (S12.6) / KAN-138 case 3: offline, a page that was never saved
 * opens the offline page, which lists what IS saved on this phone (the
 * current partition only), and each entry opens its labelled copy.
 *
 * Same ground rules as offline-snapshots.pwa.spec.js: `iphone-pwa` (the one
 * project with a live worker), the network cut by offline-proxy.js (never
 * context.setOffline()), the production CSP added by the proxy, users
 * e2e-reader / e2e-reader-b (flag on), empty cookie jar.
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

async function visit(page, net, path) {
  await expect(async () => {
    await page.goto(net.origin + path);
    await expect.poll(() => storedPaths(page), { timeout: 3000 }).toContain(path);
  }).toPass({ timeout: 20000 });
}

async function offlinePageReady(page) {
  // The worker precaches it at install; an online page load retries.
  await expect
    .poll(() =>
      page.evaluate(async () =>
        (await caches.keys()).some((name) => name.startsWith('ttstats-offline-'))
      )
    )
    .toBe(true);
}

async function readerMatchPath(page, net) {
  await page.goto(net.origin + '/pingpong/matches/');
  const hrefs = await page
    .locator('a[href^="/pingpong/matches/"]')
    .evaluateAll((links) => links.map((a) => a.getAttribute('href')));
  const path = hrefs.find((href) => /^\/pingpong\/matches\/\d+\/$/.test(href));
  if (!path) throw new Error("No seeded match in the reader's match list");
  return path;
}

const offlinePage = (page) => page.locator('[data-offline-page]');
const savedLinks = (page) => page.locator('[data-offline-saved] a[data-item-link]');

test('offline, a never-visited page lists the saved ones; each opens labelled', async ({ page, net }) => {
  await login(page, net, 'e2e-reader');
  const match = await readerMatchPath(page, net);
  for (const path of ['/pingpong/', '/pingpong/leaderboard/', match]) await visit(page, net, path);
  await offlinePageReady(page);

  net.setMode('offline');
  const response = await page.goto(net.origin + '/pingpong/calendar/');
  expect(response.headers()['x-ttstats-served-from']).toBe('offline-page');
  await expect(offlinePage(page)).toBeVisible();
  await expect(page.locator('h1')).toContainText(
    "No connection, and this page isn't saved on your phone yet.",
    { useInnerText: true }
  );
  // The URL is still the one asked for: Try again retries it.
  expect(new URL(page.url()).pathname).toBe('/pingpong/calendar/');

  // Grouped Today / Table / Matches: the four saved pages (the match list
  // was opened to find the match) and nothing else, newest first per group.
  await expect(savedLinks(page)).toHaveCount(4);
  const groups = await page
    .locator('[data-offline-saved] [data-offline-group]')
    .evaluateAll((sections) => sections.map((s) => s.getAttribute('data-offline-group')));
  expect(groups).toEqual(['today', 'table', 'matches']);
  const hrefs = await savedLinks(page).evaluateAll((links) => links.map((a) => a.getAttribute('href')));
  expect(hrefs).toEqual(['/pingpong/', '/pingpong/leaderboard/', match, '/pingpong/matches/']);
  await expect(page.locator('[data-offline-group="table"]')).toContainText('The Table');
  await expect(savedLinks(page).first()).toContainText(/Saved \d\d:\d\d/);
  // The real list replaces the two static guesses.
  await expect(page.locator('[data-offline-static]')).toBeHidden();
  await expect(page.locator('[data-offline-empty]')).toBeHidden();
  expect(await page.evaluate(() => window.__cspViolations)).toEqual([]);

  // "Try again" reloads the same URL: still no network, still this page.
  await Promise.all([page.waitForEvent('load'), page.locator('[data-offline-retry]').click()]);
  await expect(offlinePage(page)).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/pingpong/calendar/');

  // Tapping a saved page opens its copy, with the banner.
  await savedLinks(page).nth(2).scrollIntoViewIfNeeded();
  await Promise.all([page.waitForURL(net.origin + match), savedLinks(page).nth(2).click()]);
  expect(await page.evaluate(() => document.documentElement.dataset.freshness)).toBe('snapshot');
  await expect(page.locator('[data-snapshot-banner]')).toBeVisible();
  await expect(page.locator('main')).toContainText('E2E Reader');
});

test("the list is only this person's, never the previous one's", async ({ page, net }) => {
  await login(page, net, 'e2e-reader');
  const match = await readerMatchPath(page, net);
  for (const path of ['/pingpong/leaderboard/', match]) await visit(page, net, path);

  await Promise.all([
    page.waitForURL(/\/accounts\/login\//),
    page.evaluate(() => document.querySelector('form[data-sw-purge]').requestSubmit()),
  ]);
  await login(page, net, 'e2e-reader-b');
  await visit(page, net, '/pingpong/');
  await offlinePageReady(page);

  net.setMode('offline');
  await page.goto(net.origin + '/pingpong/calendar/');
  await expect(offlinePage(page)).toBeVisible();
  await expect(savedLinks(page)).toHaveCount(1);
  expect(await savedLinks(page).getAttribute('href')).toBe('/pingpong/');
  expect(await page.content()).not.toContain(match);
});

test('dark and light: the page follows the system theme', async ({ page, net }) => {
  await login(page, net, 'e2e-reader');
  await offlinePageReady(page);
  net.setMode('offline');
  const background = () =>
    page.evaluate(() => getComputedStyle(document.body).backgroundColor);

  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  await page.goto(net.origin + '/pingpong/calendar/');
  await expect(offlinePage(page)).toBeVisible();
  expect(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches)).toBe(true);
  const dark = await background();

  await page.emulateMedia({ colorScheme: 'light' });
  const light = await background();
  expect(dark).not.toBe(light);
  // Dark is the court token (#0b1220 family), light the bone one.
  const lum = (rgb) => rgb.match(/\d+/g).slice(0, 3).reduce((a, b) => a + Number(b), 0);
  expect(lum(dark)).toBeLessThan(lum(light));
});

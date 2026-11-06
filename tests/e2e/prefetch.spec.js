const { test, expect, devices } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const { applyProdCSP } = require('./helpers');
const { watchPrefetches, tab, tapTabExpectReuse } = require('./prefetch-helpers');
const { recordPageReveal, pageReveal } = require('./view-transitions');

/**
 * Tap-intent prefetch (KAN-124), iphone-safari (WebKit).
 *
 * prefetch.js fetches a tab's page on touchstart; the server makes that one
 * response cacheable for 5 s; the click's navigation is then a cache hit.
 * WebKit only reuses a FINISHED prefetch, so a click that lands mid-prefetch
 * is held until it finishes.
 *
 * Playwright's default WebKit context is ephemeral and has NO network cache
 * (like Safari private browsing), so reuse is proven in a persistent context,
 * which is what Safari and the installed PWA actually run in. The ephemeral
 * context proves the other half: with nothing to reuse, prefetch.js notices
 * and switches itself off.
 */
const AUTH_FILE = path.join(__dirname, '.auth', 'user.json');

async function persistentPhone(playwright, baseURL, testInfo) {
  const { defaultBrowserType, ...iphone } = devices['iPhone 13'];
  const context = await playwright.webkit.launchPersistentContext(
    testInfo.outputPath('profile'),
    { ...iphone, baseURL, serviceWorkers: 'block' },
  );
  await context.addCookies(JSON.parse(fs.readFileSync(AUTH_FILE, 'utf8')).cookies);
  return context;
}

test.describe('with a disk cache (Safari, the installed PWA)', () => {
  test('a tapped tab is answered by its prefetch, every time', async ({ playwright, baseURL }, testInfo) => {
    const context = await persistentPhone(playwright, baseURL, testInfo);
    const page = context.pages()[0] || await context.newPage();
    try {
      await page.goto('/pingpong/play/');
      await tapTabExpectReuse(page, '/pingpong/leaderboard/');
      // Still on: the self-check saw a cache hit and left prefetching alone.
      expect(await page.evaluate(() => sessionStorage.getItem('ttstats-prefetch-off'))).toBeNull();
      await tapTabExpectReuse(page, '/pingpong/championships/');
      await tapTabExpectReuse(page, '/pingpong/');
    } finally {
      await context.close();
    }
  });

  test('a held, prefetched navigation still runs the view transition', async ({ playwright, baseURL }, testInfo) => {
    // The held click ends in location.assign(): still a real same-origin
    // navigation, so KAN-125's cross-document transition still fires.
    const context = await persistentPhone(playwright, baseURL, testInfo);
    const page = context.pages()[0] || await context.newPage();
    try {
      await recordPageReveal(page);
      await page.goto('/pingpong/play/');
      await tapTabExpectReuse(page, '/pingpong/leaderboard/');
      expect((await pageReveal(page)).revealed).toBe(true);
    } finally {
      await context.close();
    }
  });
});

test.describe('in the default (ephemeral) context', () => {
  test('a tap prefetches the tab once and the tab still opens', async ({ page }) => {
    await page.goto('/pingpong/play/');
    const prefetches = watchPrefetches(page);
    await Promise.all([page.waitForURL('**/pingpong/leaderboard/'), tab(page, '/pingpong/leaderboard/').tap()]);
    expect(prefetches.map((r) => new URL(r.url()).pathname)).toEqual(['/pingpong/leaderboard/']);
  });

  test('with no HTTP cache to reuse, it switches itself off for the session', async ({ page }) => {
    await page.goto('/pingpong/play/');
    await Promise.all([page.waitForURL('**/pingpong/leaderboard/'), tab(page, '/pingpong/leaderboard/').tap()]);
    await page.waitForLoadState('load');
    expect(await page.evaluate(() => sessionStorage.getItem('ttstats-prefetch-off'))).toBe('1');

    const prefetches = watchPrefetches(page);
    await Promise.all([page.waitForURL('**/pingpong/championships/'), tab(page, '/pingpong/championships/').tap()]);
    expect(prefetches).toHaveLength(0);
  });
});

test.describe('what is never prefetched', () => {
  test('links outside the allowlist, opted-out links and the code denylist', async ({ page }) => {
    await page.goto('/pingpong/players/');
    await page.waitForLoadState('load');
    // Ordinary links (no data-prefetch) and markup that opts in but must
    // still be refused by prefetch.js itself.
    await page.evaluate(() => {
      const box = document.createElement('div');
      box.id = 'prefetch-fixtures';
      const links = [
        ['plain', '/pingpong/matches/', {}],
        ['opted-out', '/pingpong/calendar/', { 'data-prefetch': '', 'data-no-prefetch': '' }],
        ['live', '/pingpong/matches/1/live/', { 'data-prefetch': '' }],
        ['live-state', '/pingpong/matches/1/live/state/', { 'data-prefetch': '' }],
        ['confirm', '/pingpong/match/1/confirm/', { 'data-prefetch': '' }],
        ['join', '/pingpong/club/join/abc123/', { 'data-prefetch': '' }],
        ['logout', '/accounts/logout/', { 'data-prefetch': '' }],
        ['admin', '/admin/', { 'data-prefetch': '' }],
        ['edit', '/pingpong/players/1/edit/', { 'data-prefetch': '' }],
        ['external', 'https://example.com/pingpong/', { 'data-prefetch': '' }],
        ['new-tab', '/pingpong/ratings/', { 'data-prefetch': '', target: '_blank' }],
        ['download', '/pingpong/head-to-head/', { 'data-prefetch': '', download: '' }],
      ];
      for (const [id, href, attrs] of links) {
        const a = document.createElement('a');
        a.id = `pf-${id}`;
        a.href = href;
        a.textContent = id;
        for (const [k, v] of Object.entries(attrs)) a.setAttribute(k, v);
        box.appendChild(a);
      }
      document.querySelector('main').prepend(box);
    });

    const prefetches = watchPrefetches(page);
    for (const a of await page.locator('#prefetch-fixtures a').all()) {
      await a.dispatchEvent('touchstart');
      await a.dispatchEvent('mousedown', { button: 0 });
    }
    // Positive control: the same gestures on a tab do prefetch, so silence
    // above is the denylist working, not the events going nowhere.
    await tab(page, '/pingpong/leaderboard/').dispatchEvent('touchstart');
    await expect.poll(() => prefetches.length).toBe(1);
    await page.waitForTimeout(300);
    expect(prefetches.map((r) => new URL(r.url()).pathname)).toEqual(['/pingpong/leaderboard/']);
  });

  test('the page you are on is not fetched again', async ({ page }) => {
    await page.goto('/pingpong/leaderboard/');
    await page.waitForLoadState('load');
    const prefetches = watchPrefetches(page);
    await tab(page, '/pingpong/leaderboard/').dispatchEvent('touchstart');
    await tab(page, '/pingpong/').dispatchEvent('touchstart');
    await expect.poll(() => prefetches.length).toBe(1);
    expect(new URL(prefetches[0].url()).pathname).toBe('/pingpong/');
  });

  test('Save-Data turns it off', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'connection', { value: { saveData: true }, configurable: true });
    });
    await page.goto('/pingpong/play/');
    await page.waitForLoadState('load');
    const prefetches = watchPrefetches(page);
    await tab(page, '/pingpong/leaderboard/').dispatchEvent('touchstart');
    await page.waitForTimeout(500);
    expect(prefetches).toHaveLength(0);
  });
});

test.describe('under the production CSP', () => {
  test('prefetches with no violation and the tab opens', async ({ page }) => {
    await applyProdCSP(page);
    const violations = [];
    page.on('console', (msg) => {
      if (/Content Security Policy|Refused to/i.test(msg.text())) violations.push(msg.text());
    });
    page.on('pageerror', (err) => violations.push(String(err)));

    await page.goto('/pingpong/play/');
    await page.waitForLoadState('load');
    const prefetches = watchPrefetches(page);
    await Promise.all([page.waitForURL('**/pingpong/leaderboard/'), tab(page, '/pingpong/leaderboard/').tap()]);
    await page.waitForLoadState('load');
    expect(prefetches.length).toBe(1);
    expect(violations, violations.join('\n')).toHaveLength(0);
  });
});

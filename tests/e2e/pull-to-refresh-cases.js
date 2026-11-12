const { test, expect } = require('@playwright/test');
const { applyProdCSP, drawer, waitForDrawerSettled } = require('./helpers');
const { emulateStandalone, pull, touch, watchForReload, indicator } = require('./pull-to-refresh-helpers');
const { serveAsSnapshot, RECENT } = require('./offline-snapshot-banner-cases');

/**
 * The pull-to-refresh cases (KAN-126), shared by every engine's spec:
 * pull-to-refresh.pwa.spec.js (WebKit, iPhone) and
 * pull-to-refresh.android.spec.js (Chromium as Pixel 7, Firefox at phone size).
 *
 * The service worker is blocked: a worker-controlled page skips page.route(),
 * and applyProdCSP is built on page.route(). Nothing here needs the worker,
 * and everything here needs the production CSP.
 *
 * Standalone is emulated by an init script (Playwright cannot install to a
 * home screen), and the gesture is synthesised in-page: see the helpers.
 */
function definePullToRefreshCases() {
  test.use({ serviceWorkers: 'block' });

  test.describe('pull to refresh, installed', () => {
    let violations;
    // The scoreboard is Alpine, which needs the 'unsafe-eval' prod sends and
    // PROD_CSP withholds on purpose; its eval refusals are not ours.
    let alpineEvalIsExpected;
    // Each engine words the refusal differently; pull_to_refresh.js has no eval.
    const ALPINE_EVAL = /Alpine Expression Error|EvalError|unsafe-eval|Function\(\) blocked/;

    test.beforeEach(async ({ page }) => {
      violations = [];
      alpineEvalIsExpected = false;
      page.on('console', (msg) => {
        if (/Content Security Policy|Refused to/i.test(msg.text())) violations.push(msg.text());
      });
      page.on('pageerror', (err) => violations.push(String(err)));
      await applyProdCSP(page);
      await emulateStandalone(page);
    });

    test.afterEach(() => {
      const ours = violations.filter((v) => !(alpineEvalIsExpected && ALPINE_EVAL.test(v)));
      expect(ours, ours.join('\n')).toHaveLength(0);
    });

    test('a long pull from the top reloads the page', async ({ page }) => {
      await page.goto('/pingpong/');
      const reload = await watchForReload(page);

      await touch(page, 'touchstart', 0);
      await touch(page, 'touchmove', 40);
      await expect(indicator(page)).toHaveText('Pull to refresh');
      await expect(indicator(page)).toHaveClass(/is-visible/);
      // Ours is live, so the browser's own pull-to-refresh is held off.
      await expect(page.locator('html')).toHaveClass(/\bptr-on\b/);
      await touch(page, 'touchmove', 140);
      await expect(indicator(page)).toHaveText('Release to refresh');
      await touch(page, 'touchend', 140);

      await reload.happened();
    });

    test('a short pull springs back without reloading', async ({ page }) => {
      await page.goto('/pingpong/');
      const reload = await watchForReload(page);

      await pull(page, { dy: 50 });

      await expect(indicator(page)).not.toHaveClass(/is-visible/);
      await reload.didNotHappen();
    });

    test('a mostly horizontal drag is not a pull, and is not cancelled', async ({ page }) => {
      await page.goto('/pingpong/');
      const reload = await watchForReload(page);

      const prevented = await pull(page, { dx: 200, dy: 120 });

      expect(prevented).toBe(false);
      await expect(indicator(page)).not.toHaveClass(/is-visible/);
      await reload.didNotHappen();
    });

    test('the live scoreboard never refreshes', async ({ page }) => {
      alpineEvalIsExpected = true;
      await page.goto('/pingpong/');
      const href = await page.getByRole('link', { name: /Resume/i }).first().getAttribute('href');
      await page.goto(href);
      await expect(page.locator('#scoreboard[data-no-pull-refresh]')).toHaveCount(1);
      const reload = await watchForReload(page);

      const prevented = await pull(page, { dy: 200 });

      // Not even a listener: a pull on the scoreboard is never cancelled, so a
      // tap there cannot be eaten.
      expect(prevented).toBe(false);
      await expect(indicator(page)).toHaveCount(0);
      await reload.didNotHappen();
    });

    test('a form with unsaved input does not refresh', async ({ page }) => {
      await page.goto('/pingpong/matches/add/');
      await page.locator('main form[method="post"] textarea[name="notes"]').fill('Unsaved');
      await page.evaluate(() => window.scrollTo(0, 0));
      const reload = await watchForReload(page);

      await pull(page, { dy: 200 });

      await expect(indicator(page)).not.toHaveClass(/is-visible/);
      await reload.didNotHappen();
    });

    test('an untouched form still refreshes', async ({ page }) => {
      await page.goto('/pingpong/matches/add/');
      const reload = await watchForReload(page);

      await pull(page, { dy: 200 });

      await reload.happened();
    });

    test('an open drawer does not refresh', async ({ page }) => {
      await page.goto('/pingpong/');
      await page.locator('#mobile-menu-open').tap();
      await expect(drawer(page)).toBeVisible();
      await waitForDrawerSettled(page);
      const reload = await watchForReload(page);

      await pull(page, { dy: 200 });

      await expect(indicator(page)).not.toHaveClass(/is-visible/);
      await reload.didNotHappen();
    });

    test('offline, a pull says so instead of reloading', async ({ page }) => {
      await page.goto('/pingpong/');
      await page.evaluate(() => {
        Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false });
      });
      const reload = await watchForReload(page);

      await pull(page, { dy: 200 });

      await expect(indicator(page)).toHaveText('No connection');
      await expect(indicator(page)).toHaveAttribute('aria-live', 'polite');
      await reload.didNotHappen();
      // And it gets out of the way again.
      await expect(indicator(page)).not.toHaveClass(/is-visible/, { timeout: 4000 });
    });
  });

  test.describe('pull to refresh, on a saved copy (KAN-146)', () => {
    test.beforeEach(async ({ page }) => {
      await applyProdCSP(page);
      await emulateStandalone(page);
    });

    test('offline, a pull still reloads: the worker tries the network again', async ({ page }) => {
      // The copy the worker would serve (workers are blocked here: see above).
      await serveAsSnapshot(page, RECENT);
      await page.goto('/pingpong/');
      await expect(page.locator('html')).toHaveAttribute('data-freshness', 'snapshot');
      await page.evaluate(() => {
        Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false });
      });
      const reload = await watchForReload(page);

      await pull(page, { dy: 200 });

      await reload.happened();
    });
  });

  test.describe('pull to refresh, in the browser', () => {
    test('is absent when the app is not installed', async ({ page }) => {
      await applyProdCSP(page);
      await page.goto('/pingpong/');
      const reload = await watchForReload(page);

      const prevented = await pull(page, { dy: 200 });

      expect(prevented).toBe(false);
      await expect(indicator(page)).toHaveCount(0);
      // The browser keeps its native gesture: nothing touches overscroll.
      await expect(page.locator('html')).not.toHaveClass(/\bptr-on\b/);
      expect(await page.evaluate(() => getComputedStyle(document.documentElement).overscrollBehaviorY)).toBe('auto');
      await reload.didNotHappen();
    });
  });
}

module.exports = { definePullToRefreshCases };

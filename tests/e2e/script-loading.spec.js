const { test, expect } = require('@playwright/test');

/**
 * htmx and Alpine are both `defer` (KAN-122), so neither blocks the parser.
 *
 * What that could break, and what this proves it didn't:
 *   - an inline script touching `window.htmx` at parse time now throws
 *     "htmx is not defined" (pytest renders the markup; only a browser runs it);
 *   - deferred scripts run in document order, so htmx must still be live by
 *     the time Alpine initialises.
 *
 * No `applyProdCSP` here: the e2e policy withholds 'unsafe-eval' on purpose
 * (csp.spec.js), which makes every Alpine expression throw and would bury the
 * errors this spec is looking for.
 */
const PAGES = [
  '/pingpong/',
  '/pingpong/play/',
  '/pingpong/leaderboard/',
  '/pingpong/matches/',
  '/pingpong/matches/add/',
  '/pingpong/championships/create/',
];

test.describe('deferred htmx', () => {
  test.beforeEach(async ({ page }) => {
    // Record whether htmx already exists when Alpine starts.
    await page.addInitScript(() => {
      document.addEventListener('alpine:init', () => {
        window.__htmxAtAlpineInit = typeof window.htmx;
      });
    });
  });

  for (const path of PAGES) {
    test(`${path} runs without script errors, htmx before Alpine`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));

      const response = await page.goto(path, { waitUntil: 'load' });
      expect(response.status()).toBe(200);

      expect(errors, errors.join('\n')).toEqual([]);
      expect(await page.evaluate(() => typeof window.htmx)).toBe('object');
      expect(await page.evaluate(() => window.__htmxAtAlpineInit)).toBe('object');
    });
  }

  test('the head has no parser-blocking script', async ({ page }) => {
    await page.goto('/pingpong/');
    const blocking = await page.evaluate(() =>
      Array.from(document.head.querySelectorAll('script[src]'))
        .filter((s) => !s.defer && !s.async)
        .map((s) => s.src),
    );
    expect(blocking).toEqual([]);
  });
});

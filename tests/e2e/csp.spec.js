const { test, expect } = require('@playwright/test');
const { expectDrawerClosed, applyProdCSP } = require('./helpers');

/**
 * Production serves a Content-Security-Policy. Dev does not.
 *
 * That difference is why the mobile drawer was broken on the live site while
 * every local check -- pytest, the manual browser pass, and the whole
 * Playwright suite -- was green: none of them had ever seen the header the
 * real server sends.
 *
 * Alpine 3's standard build compiles every expression with `new Function()`.
 * Under a script-src without 'unsafe-eval' that throws, so `x-data`,
 * `x-show` and `@click` are all dead -- while Alpine still gets far enough
 * to strip `x-cloak`. Net effect: overlays render open and their close
 * buttons do nothing.
 *
 * The header below is copied verbatim from the live response.
 */
test.describe('under the production CSP', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
  });

  test('the drawer is closed on arrival', async ({ page }) => {
    await page.goto('/pingpong/players/');
    await expectDrawerClosed(page);
  });

  test('the drawer opens and closes', async ({ page }) => {
    await page.goto('/pingpong/players/');
    await page.getByRole('button', { name: 'Open menu' }).tap();
    await expect(page.locator('#mobile-menu-panel')).toBeVisible();
    await page.getByRole('button', { name: 'Close menu' }).tap();
    await expectDrawerClosed(page);
  });

  test('no script is blocked by the policy', async ({ page }) => {
    // The generic version of the above: any CSP violation on a page means
    // some behaviour silently does not exist in production.
    const violations = [];
    page.on('console', (msg) => {
      const text = msg.text();
      if (/Content Security Policy|unsafe-eval|Refused to/i.test(text)) {
        violations.push(text);
      }
    });
    page.on('pageerror', (err) => violations.push(String(err)));

    await page.goto('/pingpong/players/');
    await page.waitForTimeout(1000);

    expect(violations, `CSP violations on the page:\n${violations.join('\n')}`)
      .toHaveLength(0);
  });
});

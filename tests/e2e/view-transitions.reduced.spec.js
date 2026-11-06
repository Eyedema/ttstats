const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const { recordPageReveal, pageReveal } = require('./view-transitions');

/**
 * Reduce Motion: no page transition at all (KAN-125). app.css re-declares
 * `@view-transition { navigation: none }` under the preference, so a tab tap
 * is the instant swap it was before. WebKit supports cross-document VT, so
 * the `false` here is the preference at work, not a missing feature: the
 * plain iphone-safari spec asserts `true` for the same tap.
 */

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await recordPageReveal(page);
});

// applyProdCSP proxies every request; a deferred script still in flight when
// the test ends would otherwise fail it with "route.fetch: Test ended".
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('a tab-to-tab navigation has no view transition', async ({ page }) => {
  await page.goto('/pingpong/');
  expect(
    await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)
  ).toBe(true);

  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Table' }).tap();
  await expect(page).toHaveURL(/\/pingpong\/leaderboard\/$/);

  expect((await pageReveal(page)).revealed).toBe(false);
});

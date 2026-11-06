const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const { recordPageReveal, pageReveal } = require('./view-transitions');

/**
 * Dark mode: what shows behind the transition snapshots is the court, so a
 * tab tap never flashes white (KAN-125).
 */

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
  await recordPageReveal(page);
});

// applyProdCSP proxies every request; a deferred script still in flight when
// the test ends would otherwise fail it with "route.fetch: Test ended".
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('the view-transition root is the dark court', async ({ page }) => {
  await page.goto('/pingpong/');
  expect(
    await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches)
  ).toBe(true);

  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Cups' }).tap();
  await expect(page).toHaveURL(/\/championships\/$/);

  const reveal = await pageReveal(page);
  expect(reveal.revealed).toBe(true);
  expect(reveal.background).toBe('rgb(11, 18, 32)');
});

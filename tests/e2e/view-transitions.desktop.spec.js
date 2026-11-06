const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const { recordPageReveal, pageReveal, renderedTransitionNames, transitionNameOf } = require('./view-transitions');

/**
 * Cross-document view transitions in the wide layout (KAN-125). The header
 * and tab bar are display:none here, so the sidebar is the chrome that stays
 * put, and it must be the only named piece of chrome on the page.
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

test('a sidebar navigation is revealed through a view transition', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/pingpong/');

  await page.locator('aside').getByRole('link', { name: 'Table', exact: true }).first().click();
  await expect(page).toHaveURL(/\/pingpong\/leaderboard\/$/);

  expect((await pageReveal(page)).revealed).toBe(true);
  expect(errors).toEqual([]);
});

test('only the sidebar is named; the hidden mobile chrome is not captured', async ({ page }) => {
  await page.goto('/pingpong/leaderboard/');
  expect(await transitionNameOf(page, 'aside')).toBe('app-sidebar');

  const names = await renderedTransitionNames(page);
  expect(names).toContain('app-sidebar');
  for (const hidden of ['app-header', 'tab-bar', 'page-title', 'app-drawer']) {
    expect(names).not.toContain(hidden);
  }
  expect(new Set(names).size).toBe(names.length);
});

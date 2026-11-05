const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/** The desktop sidebar shows the Glicko rating with its uncertainty (KAN-71). */
test.use({ storageState: path.join(__dirname, '.auth', 'glicko.json') });

test('the sidebar rating carries the ± and the menu links the explainer', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await applyProdCSP(page);
  await page.goto('/pingpong/leaderboard/');

  const rating = page.locator('[data-nav-rating]:visible').first();
  await expect(rating).toBeVisible();
  await expect(rating).toHaveAttribute('data-rating-system', 'glicko2');
  expect(await rating.innerText()).toMatch(/\d{4}\s*±\d+/);
  expect(await rating.innerText()).not.toMatch(/\bElo\b/);

  await page.locator('nav:visible, aside:visible').getByRole('link', { name: 'How ratings work' }).first().click();
  await expect(page).toHaveURL(/\/pingpong\/ratings\/$/);
  expect(errors).toEqual([]);
});

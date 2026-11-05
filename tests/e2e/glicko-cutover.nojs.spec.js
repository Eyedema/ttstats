const path = require('path');
const { test, expect } = require('@playwright/test');

/**
 * The Glicko-2 surfaces with scripting off (KAN-71). The ± and the markers are
 * server-rendered text, the rating help is a native <details> that renders
 * closed, and the explainer is plain prose: none of it may need JavaScript.
 */
test.use({ storageState: path.join(__dirname, '.auth', 'glicko.json') });

test('the leaderboard still carries the uncertainty as text', async ({ page }) => {
  await page.goto('/pingpong/leaderboard/');
  const row = page.locator('[data-rated-row]', { hasText: 'E2E Glicko Player' });
  await expect(row.locator('[data-rating-rd]')).toHaveText(/^±\d+$/);
  await expect(row.locator('[data-rating-status="provisional"]')).toBeVisible();
  await expect(page.locator('#rating-help')).not.toHaveAttribute('open', '');
});

test('the explainer opens from the help link without JavaScript', async ({ page }) => {
  await page.goto('/pingpong/leaderboard/');
  const link = page.locator('#leaderboard-results [data-how-ratings-link]').last();
  // Centre it first: the minimal scroll Playwright does parks it under the fixed tab bar.
  await link.evaluate((el) => el.scrollIntoView({ block: 'center' }));
  await Promise.all([page.waitForURL(/\/pingpong\/ratings\/$/), link.tap()]);
  await expect(page.getByRole('heading', { name: 'How ratings work' })).toBeVisible();
});

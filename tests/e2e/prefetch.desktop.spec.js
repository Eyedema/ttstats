const { test } = require('@playwright/test');
const { tapTabExpectReuse } = require('./prefetch-helpers');

/**
 * desktop-chrome (KAN-124): a mouse click's mousedown starts the prefetch,
 * and Chromium answers the click's navigation with it.
 */
test('a sidebar click is answered by its mousedown prefetch', async ({ page }) => {
  await page.goto('/pingpong/play/');
  const row = page.locator('aside a[data-prefetch][href="/pingpong/leaderboard/"]');
  // tapTabExpectReuse taps; on desktop use click via a thin wrapper.
  await tapTabExpectReuse(page, '/pingpong/leaderboard/', { tap: () => row.click() });
});

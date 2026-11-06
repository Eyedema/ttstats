const { test, expect } = require('@playwright/test');
const { watchPrefetches, tab } = require('./prefetch-helpers');

/**
 * javaScriptEnabled: false. prefetch.js is the only thing that sends
 * `Purpose: prefetch`; with no JS a tab is an ordinary link (KAN-124).
 */
test('with JavaScript off a tab is a plain link: no prefetch, it still navigates', async ({ page }) => {
  await page.goto('/pingpong/play/');
  const prefetches = watchPrefetches(page);
  await Promise.all([page.waitForURL('**/pingpong/leaderboard/'), tab(page, '/pingpong/leaderboard/').tap()]);
  expect(prefetches).toHaveLength(0);
  await expect(tab(page, '/pingpong/leaderboard/')).toHaveAttribute('aria-current', 'page');
});

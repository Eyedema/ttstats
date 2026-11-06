const { test, expect } = require('@playwright/test');
const { tapTabExpectReuse } = require('./prefetch-helpers');

/**
 * Tap-intent prefetch on Android engines (KAN-124): `android-chrome`
 * (Chromium, Pixel 7) and `android-firefox` (Gecko at a phone viewport with
 * touch -- an approximation, Playwright's Firefox has no isMobile).
 *
 * Chromium joins the navigation to the in-flight prefetch; Gecko reuses it
 * once finished, so prefetch.js holds the click until then. Either way the
 * page shown must be the prefetched response.
 */
test('a tapped tab is answered by its prefetch', async ({ page }) => {
  await page.goto('/pingpong/play/');
  await tapTabExpectReuse(page, '/pingpong/leaderboard/');
  await tapTabExpectReuse(page, '/pingpong/championships/');
  await tapTabExpectReuse(page, '/pingpong/');
  // The held-click self-check (Gecko) saw cache hits and left it on.
  expect(await page.evaluate(() => sessionStorage.getItem('ttstats-prefetch-off'))).toBeNull();
});

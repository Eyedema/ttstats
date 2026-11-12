const { test, expect } = require('@playwright/test');

/**
 * javaScriptEnabled: false (KAN-135). The toast region and the "Slow
 * connection" line exist only once connection.js has run, so with no JS
 * there is nothing on screen and no connection state: fail closed.
 */
test('with JavaScript off there is no toast, no hint and no connection state', async ({ page }) => {
  await page.goto('/pingpong/leaderboard/');
  await expect(page.locator('#id_season')).toBeVisible();
  await expect(page.locator('#ttstats-toasts')).toHaveCount(0);
  await expect(page.locator('[data-toast]')).toHaveCount(0);
  await expect(page.locator('#connection-hint')).toHaveCount(0);
  expect(await page.locator('html').getAttribute('data-connection')).toBeNull();
});

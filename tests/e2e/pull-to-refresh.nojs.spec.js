const { test, expect } = require('@playwright/test');

/**
 * Runs with JavaScript disabled (KAN-126). The pull-to-refresh indicator is
 * created by its script, so with no script there is nothing to fail open --
 * and no overscroll containment taken from the browser either.
 */
test('no pull-to-refresh indicator without JavaScript', async ({ page }) => {
  await page.goto('/pingpong/');
  await expect(page.locator('#pull-refresh, .ptr')).toHaveCount(0);
  await expect(page.locator('html')).not.toHaveClass(/\bptr-on\b/);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
});

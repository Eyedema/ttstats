const path = require('path');
const { test, expect } = require('@playwright/test');

/**
 * The seasons filters with scripting off (KAN-48). htmx is an enhancement:
 * the form is a plain GET with an Apply button inside <noscript>, and the
 * rating explanation is a native <details> that renders closed.
 */
test.use({ storageState: path.join(__dirname, '.auth', 'seasons.json') });

test('the filters still work without JavaScript', async ({ page }) => {
  await page.goto('/pingpong/leaderboard/');
  await expect(page.locator('#rating-help')).not.toHaveAttribute('open', '');

  await page.locator('#id_season').selectOption('all');
  await Promise.all([
    page.waitForURL(/season=all/),
    page.getByRole('button', { name: 'Apply' }).tap(),
  ]);
  await expect(page.locator('[data-scope-label]')).toContainText('Singles · all-time');
});

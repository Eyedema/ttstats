const { test, expect } = require('@playwright/test');
const { serveAsSnapshot, RECENT } = require('./offline-snapshot-banner-cases');

/**
 * Read-only mode (KAN-146) with JavaScript off: the saved copy still says
 * what it is, by CSS alone, and nothing JS would add exists -- no relative
 * age, no "You're back online", no Refresh button (fail closed).
 *
 * Workers are blocked in this project, so the copy is built the way sw.js
 * builds it (offline-snapshot-banner-cases.js). Logged in as e2e-reader, who
 * has the snapshot flag on, so the page really carries snapshot_mode.js and
 * its patterns: the check is about them not running, not about their absence.
 */
test.use({ storageState: { cookies: [], origins: [] } });

test('a saved copy with no JS: absolute time only, no Refresh', async ({ page }) => {
  await page.goto('/accounts/login/');
  await page.fill('input[name="username"]', 'e2e-reader');
  await page.fill('input[name="password"]', 'e2e-local-only');
  await Promise.all([page.waitForURL(/\/pingpong\//), page.click('button[type="submit"]')]);

  await serveAsSnapshot(page, RECENT);
  await page.goto('/pingpong/');
  await expect(page.locator('script[src*="snapshot_mode"]')).toHaveCount(1);
  await expect(page.locator('#snapshot-paths')).toHaveCount(1);

  const banner = page.locator('[data-snapshot-banner]');
  await expect(banner).toBeVisible();
  await expect(banner).toHaveText('No connection — saved copy from 14:32', { useInnerText: true });
  await expect(page.locator('[data-snapshot-relative]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Refresh' })).toHaveCount(0);
  await expect(page.getByText('back online')).toHaveCount(0);
});

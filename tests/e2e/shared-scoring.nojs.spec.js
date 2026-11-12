const { test, expect } = require('@playwright/test');

/**
 * Runs with javaScriptEnabled: false (KAN-151 / KAN-164).
 *
 * The lobby must work with no script at all: the button is a plain form
 * submit, the QR code is server-rendered SVG, the link is selectable text and
 * "Check again" is a reload. Share and Copy depend on the script, so they
 * must stay hidden (fail closed) rather than render as dead buttons.
 */

test('the lobby works without JavaScript', async ({ page }) => {
  await page.goto('/pingpong/matches/add/');
  await Promise.all([page.waitForURL(/\/live\/lobby\/\d+\/$/), page.getByTestId('score-shared').tap()]);
  await expect(page).toHaveURL(/\/pingpong\/live\/lobby\/\d+\/$/);
  const lobbyUrl = page.url();

  await expect(page.getByTestId('lobby-qr').locator('svg.qr')).toBeVisible();
  await expect(page.getByTestId('lobby-url')).toHaveText(/\/pingpong\/live\/join\/[0-9a-f]{32}\/$/);

  await expect(page.locator('[data-lobby-share]')).toBeHidden();
  await expect(page.locator('[data-lobby-copy]')).toBeHidden();

  const again = page.getByTestId('check-again');
  // A fresh load: the form POST's view transition can still cover the page
  // in WebKit with scripting off, and intercepts the tap.
  await page.goto(lobbyUrl);
  await again.evaluate((el) => el.scrollIntoView({ block: 'center' }));
  await Promise.all([page.waitForEvent('load'), again.tap()]);
  await expect(page).toHaveURL(lobbyUrl);
  await expect(page.getByTestId('lobby-qr').locator('svg.qr')).toBeVisible();
});

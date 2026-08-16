const { test, expect } = require('@playwright/test');
const { expectDrawerClosed } = require('./helpers');

/**
 * Runs with javaScriptEnabled: false.
 *
 * The strongest possible statement of the rule the drawer broke: UI whose
 * hidden state is managed by a framework must still be hidden when that
 * framework is not running at all. No JS is the limit case of "Alpine failed",
 * and it is trivially reproducible, unlike whatever iOS Safari was doing.
 *
 */
test('drawer is not on screen when JavaScript is unavailable', async ({ page }) => {
  // Session comes from the saved storage state, so no form POST is needed.
  await page.goto('/pingpong/players/');
  await expectDrawerClosed(page);

  // And the page underneath must still be operable.
  await expect(page.getByRole('heading', { name: 'Players' })).toBeVisible();
});

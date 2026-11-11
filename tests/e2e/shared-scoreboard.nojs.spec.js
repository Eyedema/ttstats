const { test, expect } = require('@playwright/test');

/**
 * The shared scoreboard (KAN-153) with scripting off: like the single-scorer
 * one it fails closed (every overlay and the board stay hidden, the page
 * says it needs JavaScript), and the server-rendered markup already holds the
 * "my side" layout: data-shared on the root, the opponent's zone a plain div.
 */

const HIDDEN = [
  '[data-sync-indicator]', '[data-sync-sheet]', '[data-leave-sheet]', '[data-sb-finish]',
  '[data-sb-reauth]', '[data-sb-follow]', '[data-sb-switch]', '[data-sb-toast]',
  '[data-sb-picker]', '[data-sb-board]',
  // KAN-154: presence, the takeover bar, the Phones and Remove sheets, the
  // removed banner.
  '[data-presence="team1"]', '[data-presence="team2"]', '[data-sb-shared-actions]',
  '[data-action="phones"]', '[data-phones-sheet]', '[data-remove-sheet]', '[data-sb-removed]',
];

test('the shared scoreboard fails closed without JavaScript', async ({ page }) => {
  await page.goto('/pingpong/');
  await page.locator('section', { hasText: 'E2E Partner' })
    .getByTestId('live-resume-link').first().click();
  const root = page.locator('#scoreboard');
  await expect(root).toBeVisible();
  await expect(root).toHaveAttribute('data-shared', '');
  await expect(root).toHaveAttribute('data-my-side', 'team1');
  for (const selector of HIDDEN) {
    await expect(page.locator(selector), selector).toBeHidden();
  }
  expect(await page.locator('[data-zone="theirs"]').evaluate((el) => el.tagName)).toBe('DIV');
  // WebKit with javaScriptEnabled:false still parses <noscript> as raw text;
  // asserted as markup, as scoreboard-offline.nojs.spec.js does.
  expect(await page.locator('#scoreboard noscript').innerHTML()).toContain('needs JavaScript');
});

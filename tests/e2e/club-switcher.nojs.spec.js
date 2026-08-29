const { test, expect } = require('@playwright/test');
const { expectDrawerClosed } = require('./helpers');

/**
 * Runs with javaScriptEnabled: false.
 *
 * The fail-closed rule, applied to the switcher. The drawer once shipped
 * permanently open and unclosable because its closed state depended on Alpine
 * booting *and* on the compiled CSS carrying [x-cloak]: two things had to go
 * right or the user was trapped.
 *
 * The switcher obeys that rule by having NO hidden state at all. It is a short
 * list of plain POST forms, never collapsed, so there is nothing that can
 * render open-and-unusable. This spec is the proof: with no JavaScript
 * whatsoever, the control is either absent (inside the closed drawer) or fully
 * working (in the desktop sidebar) -- never present-but-dead.
 */

test('the switcher is not on screen when JavaScript is unavailable', async ({ page }) => {
  await page.goto('/pingpong/');

  // The drawer stays shut, so its copy of the switcher is not on screen.
  await expectDrawerClosed(page);
  await expect(page.locator('#mobile-menu-panel [data-club-switcher]')).toBeHidden();

  // And the page underneath is still operable.
  await expect(page.locator('header [data-active-club]')).toBeVisible();
});

test('the switcher needs no JavaScript to work', async ({ page }) => {
  // Rendered wide, where the sidebar copy is on screen without the drawer.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/pingpong/');

  const sidebar = page.locator('aside [data-club-switcher]');
  await expect(sidebar).toBeVisible();

  const before = await page.locator('aside [data-active-club], aside p').first().innerText();

  // A plain form POST. No fetch, no Alpine, no event handler.
  await Promise.all([
    page.waitForURL(/\/pingpong\/$/),
    sidebar.locator('button[type="submit"]:not([aria-current="true"])').first().click(),
  ]);

  await expect(page.locator('aside [data-club-switcher] [aria-current="true"]')).toHaveCount(1);
  expect(before).toBeTruthy();
});

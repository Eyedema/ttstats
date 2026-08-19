const { test, expect } = require('@playwright/test');

/**
 * Runs with javaScriptEnabled: false.
 *
 * The navigation progress bar is a fixed, full-width, top-of-viewport element,
 * which puts it in the same category as the drawer: if it renders when it
 * should not, it sits over the mobile header. So it obeys the same rule --
 * closed is the static, JS-free render, and only script can arm it.
 *
 * With no JS the browser draws its own loading UI anyway, so there is nothing
 * to replace and nothing to show.
 */
test('the progress bar stays inert when JavaScript is unavailable', async ({ page }) => {
  await page.goto('/pingpong/players/');

  const bar = page.locator('#nav-progress');
  await expect(bar).toHaveCount(1);
  await expect(bar).not.toHaveClass(/is-loading/);
  await expect(bar).toHaveCSS('opacity', '0');

  // And it must not be intercepting taps meant for the page.
  await expect(bar).toHaveCSS('pointer-events', 'none');

  // The page underneath is still operable.
  await expect(page.getByRole('heading', { name: 'Players' })).toBeVisible();
});

test('tap feedback is pure CSS and survives with no JavaScript', async ({ page }) => {
  await page.goto('/pingpong/players/');

  // The :active acknowledgement is the one piece of responsiveness that must
  // never depend on scripting -- it is what the user sees before anything
  // else has had a chance to run.
  const tab = page.getByRole('link', { name: 'Table' });
  await expect(tab).toHaveCSS('touch-action', 'manipulation');
  await expect(tab).toHaveClass(/pressable-flat/);
});

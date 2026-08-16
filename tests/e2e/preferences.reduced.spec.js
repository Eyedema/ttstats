const { test, expect } = require('@playwright/test');
const { drawer } = require('./helpers');

/**
 * Runs with reducedMotion: 'reduce'.
 *
 * Previously "verified by inspection" -- i.e. not verified. A great many
 * iPhone users have Reduce Motion switched on, so this is a mainstream
 * configuration, not an edge case, and the drawer's open/close path has to
 * work in it.
 */
test.describe('reduced motion', () => {
  test.beforeEach(async ({ page }) => {
    // Set explicitly rather than relying on the project's `use.reducedMotion`,
    // which did not reach matchMedia in WebKit. The first spec below asserts
    // the preference actually landed, so this can never silently regress into
    // a suite that thinks it is testing reduced motion and is not.
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/pingpong/players/');
  });

  test('the preference actually reaches the page', async ({ page }) => {
    const matches = await page.evaluate(
      () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
    expect(matches).toBe(true);
  });

  test('transforms are stripped but colour feedback survives', async ({ page }) => {
    // The rule narrows transition-property rather than zeroing durations, so
    // colour still animates and nothing travels through space.
    const props = await page.evaluate(() => {
      const el = document.querySelector('a[href*="leaderboard"]');
      return getComputedStyle(el).transitionProperty;
    });
    expect(props).toContain('opacity');
    expect(props).not.toContain('transform');
  });

  test('the drawer still opens and closes', async ({ page }) => {
    // The failure this guards against: a transition that never fires, so
    // Alpine never reaches the end state and the panel sticks half-open.
    await page.getByRole('button', { name: 'Open menu' }).tap();
    await expect(drawer(page)).toBeVisible();

    await page.getByRole('button', { name: 'Close menu' }).tap();
    await expect(drawer(page)).toBeHidden();
  });

  test('nothing loops indefinitely', async ({ page }) => {
    // animate-pulse and friends should settle on a final frame, not oscillate.
    const infinite = await page.evaluate(() =>
      [...document.querySelectorAll('*')].filter(
        (el) => getComputedStyle(el).animationIterationCount === 'infinite'
      ).length
    );
    expect(infinite).toBe(0);
  });
});

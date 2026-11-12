const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const { openSharedScoreboard, startIfNeeded } = require('./shared-scoreboard-helpers');

/**
 * Reduced motion (KAN-153): the pulse on the opponent's digits when their
 * point arrives is an animation, so it is off; the digits themselves change
 * all the same. The layout does not depend on motion.
 */

test('the opponent-point pulse does not animate with reduced motion', async ({ page }) => {
  // Explicit, as momentum.reduced.spec.js does: the project's
  // use.reducedMotion does not reach matchMedia in WebKit.
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await applyProdCSP(page);
  await openSharedScoreboard(page);
  expect(await page.evaluate(
    () => matchMedia('(prefers-reduced-motion: reduce)').matches,
  )).toBe(true);
  await startIfNeeded(page);
  const digits = page.locator('[data-zone="theirs"] [data-score]');
  // What renderShared() does when their point arrives.
  const animation = await digits.evaluate((el) => {
    el.classList.add('score-pulse');
    return getComputedStyle(el).animationName;
  });
  expect(animation).toBe('none');
  await expect(page.locator('#scoreboard')).toHaveAttribute('data-shared', '');
  await expect(page.locator('[data-zone="mine"]')).toBeVisible();
});

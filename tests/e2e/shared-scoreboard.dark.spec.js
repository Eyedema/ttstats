const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const { openSharedScoreboard, startIfNeeded } = require('./shared-scoreboard-helpers');

/**
 * The "my side" layout (KAN-153) in dark, the palette's base: both zones wear
 * their player's hue, the digits read against it, and nothing is amber
 * (amber means live right now, and it is not a side's colour).
 */

const AMBER = 'rgb(245, 158, 11)';

test('both zones are legible in dark and never amber', async ({ page }) => {
  await applyProdCSP(page);
  await openSharedScoreboard(page);
  expect(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches)).toBe(true);
  await startIfNeeded(page);
  for (const selector of ['[data-zone="mine"]', '[data-zone="theirs"]']) {
    const colours = await page.locator(selector).evaluate((el) => {
      const zone = getComputedStyle(el);
      const digits = getComputedStyle(el.querySelector('[data-score]'));
      return { bg: zone.backgroundColor, fg: digits.color };
    });
    expect(colours.bg, selector).not.toBe(AMBER);
    expect(colours.fg, selector).not.toBe(colours.bg);
  }
  await page.screenshot({ path: test.info().outputPath('shared-my-side-dark.png') });
});

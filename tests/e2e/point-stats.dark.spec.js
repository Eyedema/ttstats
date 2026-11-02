const { test, expect } = require('@playwright/test');
const { applyProdCSP, openPlayerByName } = require('./helpers');

/** The point-stats panel in DARK, this palette's base rather than its variant. */

function rgb(value) {
  return value.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
}
const luminance = ([r, g, b]) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

test('the dark media query actually reached the page', async ({ page }) => {
  await page.goto('/pingpong/');
  expect(
    await page.evaluate(() => window.matchMedia('(prefers-color-scheme: dark)').matches)
  ).toBe(true);
});

test('the big rate reads light against the dark card', async ({ page }) => {
  await openPlayerByName(page, 'E2E Momentum');
  const heading = page.getByRole('heading', { name: /Serving & receiving/i });
  await expect(heading).toBeVisible();

  const panel = heading.locator('xpath=ancestor::div[contains(@class,"bg-card")][1]');
  const { fg, bg } = await panel.evaluate((el) => {
    const rate = el.querySelector('.text-display');
    return {
      fg: getComputedStyle(rate).color,
      bg: getComputedStyle(el).backgroundColor,
    };
  });

  expect(Math.abs(luminance(rgb(fg)) - luminance(rgb(bg)))).toBeGreaterThan(0.3);
});

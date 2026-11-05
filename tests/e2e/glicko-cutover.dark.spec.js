const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The Glicko-2 markers and chart in DARK, the palette's base (KAN-71).
 * Amber means live right now and nowhere else, so no rating marker may be it;
 * and Chart.js renders a CSS custom property as black, so the axis colours
 * must have been resolved to a literal rgb() before they reach it.
 */
test.use({ storageState: path.join(__dirname, '.auth', 'glicko.json') });

const BALL = 'rgb(245, 158, 11)';

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

test('the dark media query reached the page', async ({ page }) => {
  await page.goto('/pingpong/leaderboard/');
  expect(
    await page.evaluate(() => window.matchMedia('(prefers-color-scheme: dark)').matches),
  ).toBe(true);
});

test('rating markers and the uncertainty are not amber', async ({ page }) => {
  await page.goto('/pingpong/leaderboard/');
  const marker = page.locator('#leaderboard-results [data-rating-status="provisional"]').first();
  await expect(marker).toBeVisible();
  for (const locator of [marker, page.locator('#leaderboard-results [data-rating-rd]').first()]) {
    const { color, border } = await locator.evaluate((el) => {
      const s = getComputedStyle(el);
      return { color: s.color, border: s.borderTopColor };
    });
    expect(color).not.toBe(BALL);
    expect(border).not.toBe(BALL);
  }
});

test('the chart axes resolve to literal rgb', async ({ page }) => {
  await page.goto('/pingpong/players/');
  const href = await page
    .locator('tr')
    .filter({ hasText: 'E2E Glicko Player' })
    .first()
    .locator('a[href^="/pingpong/players/"]:not([href*="edit"])')
    .first()
    .getAttribute('href');
  await page.goto(href);
  const canvas = page.locator('#ratingChart');
  await expect(canvas).toBeVisible();
  await expect
    .poll(() => canvas.evaluate((c) => Boolean(window.Chart && window.Chart.getChart(c))))
    .toBe(true);
  const colours = await canvas.evaluate((c) => {
    const chart = window.Chart.getChart(c);
    return [chart.options.scales.x.ticks.color, chart.options.scales.y.ticks.color];
  });
  for (const colour of colours) {
    expect(colour).toMatch(/^rgba?\(\s*\d+/);
    expect(colour).not.toBe('rgb(0, 0, 0)');
  }
});

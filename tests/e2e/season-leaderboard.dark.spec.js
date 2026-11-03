const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The provisional / unrated markers in dark (KAN-50), which is the palette's
 * base. The light pass is season-leaderboard.spec.js on `iphone-safari`.
 */
test.use({ storageState: path.join(__dirname, '.auth', 'seasons.json') });

const rgb = (s) => (s.match(/\d+/g) || []).map(Number);
const luminance = ([r, g, b]) => 0.2126 * r + 0.7152 * g + 0.0722 * b;

async function paintedBackground(locator) {
  return locator.evaluate((el) => {
    let node = el;
    let bg = getComputedStyle(node).backgroundColor;
    while (node && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
      node = node.parentElement;
      bg = node ? getComputedStyle(node).backgroundColor : 'rgb(0, 0, 0)';
    }
    return bg;
  });
}

test.describe('rating markers in dark', () => {
  test('both markers are light text on a dark surface, and not amber', async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await applyProdCSP(page);
    await page.goto('/pingpong/leaderboard/');

    const dark = await page.evaluate(
      () => window.matchMedia('(prefers-color-scheme: dark)').matches,
    );
    expect(dark).toBe(true);

    for (const status of ['provisional', 'unrated']) {
      const marker = page.locator(`#leaderboard-results [data-rating-status="${status}"]`).first();
      await expect(marker).toBeVisible();
      const color = await marker.evaluate((el) => getComputedStyle(el).color);
      const background = await paintedBackground(marker);
      expect(luminance(rgb(color))).toBeGreaterThan(luminance(rgb(background)));
      expect(color).not.toBe('rgb(245, 158, 11)');
    }
    expect(errors).toEqual([]);
  });
});

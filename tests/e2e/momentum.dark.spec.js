const { test, expect } = require('@playwright/test');
const { applyProdCSP, openMomentumMatch } = require('./helpers');

/**
 * The momentum chart in DARK, which is this palette's base rather than its
 * variant. A chart whose axis labels are drawn in the light theme's ink is
 * invisible on the navy court, and nothing in pytest or a light-mode spec
 * would ever say so.
 *
 * The two series are player HUES, which are identities and byte-identical in
 * both themes -- so what is asserted here is that they stay distinct from each
 * other and from the background, and that the axis ink follows the theme.
 */

function rgb(value) {
  return value.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
}

const luminance = ([r, g, b]) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

test.describe('momentum chart in dark mode', () => {
  test('the dark media query actually reached the page', async ({ page }) => {
    // Without this the whole file would pass in light mode for entirely the
    // wrong reason.
    await page.goto('/pingpong/');
    const isDark = await page.evaluate(
      () => window.matchMedia('(prefers-color-scheme: dark)').matches
    );
    expect(isDark).toBe(true);
  });

  test('the axis ink is light against the dark court, not the light theme ink',
    async ({ page }) => {
      await openMomentumMatch(page);
      await expect(page.getByRole('heading', { name: 'Momentum' })).toBeVisible();

      // The chart reads --bone-muted off the resolved tokens rather than
      // hard-coding a grey, which is what makes it follow all three theme
      // states. Assert the token it reads is the DARK one.
      const { ink, background } = await page.evaluate(() => {
        const root = getComputedStyle(document.documentElement);
        return {
          ink: 'rgb(' + root.getPropertyValue('--bone-muted').trim() + ')',
          background: getComputedStyle(document.body).backgroundColor,
        };
      });

      expect(luminance(rgb(ink))).toBeGreaterThan(luminance(rgb(background)) + 0.15);
    });

  test('the two series are distinguishable from each other', async ({ page }) => {
    await openMomentumMatch(page);

    const colours = await page.evaluate(() => {
      const el = document.getElementById('point-chart-data');
      return JSON.parse(el.textContent).colours;
    });

    expect(colours).toHaveLength(2);
    expect(colours[0]).not.toBe(colours[1]);
    // Both hues must stand off the navy court, or one line vanishes.
    const bg = await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor
    );
    for (const colour of colours) {
      const hex = colour.replace('#', '');
      const parts = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
      expect(Math.abs(luminance(parts) - luminance(rgb(bg)))).toBeGreaterThan(0.15);
    }
  });
});

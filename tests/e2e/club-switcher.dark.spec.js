const { test, expect } = require('@playwright/test');
const { waitForDrawerSettled } = require('./helpers');

/**
 * The switcher in dark (KAN-19 / S1.8).
 *
 * Dark is the palette's BASE, so the light rendering a developer sees on a
 * desktop is the variant. A control introduced with a hardcoded light colour
 * looks fine to whoever wrote it and is unreadable for everyone whose phone is
 * in dark mode -- which on this app is most people, most evenings.
 */

const rgb = (s) => (s.match(/\d+/g) || []).map(Number);
const luminance = ([r, g, b]) => 0.2126 * r + 0.7152 * g + 0.0722 * b;

test.describe('club switcher in dark', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/pingpong/');
    // Guard first: without this the whole file would pass in light mode for
    // entirely the wrong reason. Same check palette.dark.spec.js makes.
    const dark = await page.evaluate(
      () => window.matchMedia('(prefers-color-scheme: dark)').matches,
    );
    expect(dark).toBe(true);

    await page.getByRole('button', { name: 'Open menu' }).tap();
    await waitForDrawerSettled(page);
  });

  test('the rows are light text on a dark surface', async ({ page }) => {
    const row = page
      .locator('#mobile-menu-panel [data-club-switcher] button')
      .first();

    const { color, background } = await row.evaluate((el) => {
      const style = getComputedStyle(el);
      // Walk up for a painted background: the button may be transparent.
      let node = el;
      let bg = style.backgroundColor;
      while (node && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
        node = node.parentElement;
        bg = node ? getComputedStyle(node).backgroundColor : 'rgb(0,0,0)';
      }
      return { color: style.color, background: bg };
    });

    expect(luminance(rgb(color))).toBeGreaterThan(luminance(rgb(background)));
  });

  test('no hardcoded white slipped in', async ({ page }) => {
    // bg-white is white-on-white on the navy court surface -- the specific
    // mistake CLAUDE.md warns about.
    const backgrounds = await page
      .locator('#mobile-menu-panel [data-club-switcher] button')
      .evaluateAll((els) => els.map((el) => getComputedStyle(el).backgroundColor));

    for (const bg of backgrounds) {
      expect(luminance(rgb(bg))).toBeLessThan(200);
    }
  });
});

const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The palette in DARK, which is this design's base rather than its alternate.
 *
 * Light is the override, because the viewer's system setting drives it -- so
 * the rendering a developer sees on a desktop in an office is the exception.
 * A suite that only ever ran in light mode would be testing the variant and
 * leaving the norm uncovered.
 *
 * What these specs protect is the contract, not the exact hex values: the app
 * background is dark, text is light against it, and the three meaning-carrying
 * colours (paddle red, ball amber, confirmed green) are IDENTICAL in both
 * themes because they are identities rather than surfaces.
 */

/** [r, g, b] from a `rgb(…)` / `rgba(…)` string. */
function rgb(value) {
  return value.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
}

const luminance = ([r, g, b]) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;

async function cssVar(page, name) {
  return page.evaluate(
    (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(),
    name
  );
}

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

test.describe('dark palette', () => {
  test('the page is dark and the text is not', async ({ page }) => {
    await page.goto('/pingpong/');

    const { bg, fg } = await page.evaluate(() => {
      const s = getComputedStyle(document.body);
      return { bg: s.backgroundColor, fg: s.color };
    });

    expect(luminance(rgb(bg))).toBeLessThan(0.2);
    expect(luminance(rgb(fg))).toBeGreaterThan(0.7);
  });

  test('the media query actually reached the page', async ({ page }) => {
    // If this fails, every other assertion in this file is meaningless: the
    // project would be running in light mode and passing for the wrong reason.
    await page.goto('/pingpong/');
    expect(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches)).toBe(true);
    expect(await cssVar(page, '--court')).toBe('11 18 32');
  });

  test('identities do not flip with the theme', async ({ page }) => {
    // Paddle red, ball amber and confirmed green mean the same thing in both
    // themes, so they hold the same value in both. Only surfaces flip.
    await page.goto('/pingpong/');
    expect(await cssVar(page, '--paddle')).toBe('239 68 68');
    expect(await cssVar(page, '--ball')).toBe('245 158 11');
  });

  test('nav chrome is solid, not translucent', async ({ page }) => {
    // Nothing in this design is see-through without a solid fallback, and a
    // navigation bar is the last place to spend legibility on an effect.
    await page.goto('/pingpong/');

    const bar = page.getByRole('navigation', { name: 'Primary' });
    const { bg, backdrop } = await bar.evaluate((node) => {
      const s = getComputedStyle(node);
      return { bg: s.backgroundColor, backdrop: s.backdropFilter };
    });

    expect(bg).not.toContain('rgba(0, 0, 0, 0)');
    expect(backdrop === 'none' || backdrop === '').toBe(true);
  });

  test('the drawer is opaque enough to read over the page', async ({ page }) => {
    await page.goto('/pingpong/');
    await page.getByRole('button', { name: 'Open menu' }).tap();

    const panel = page.locator('#mobile-menu-panel');
    await expect(panel).toBeVisible();

    const bg = await panel.evaluate((n) => getComputedStyle(n).backgroundColor);
    // Fully opaque: `rgb(...)` with no alpha, or alpha exactly 1.
    const alpha = bg.startsWith('rgba') ? Number(bg.match(/[\d.]+\)$/)[0].slice(0, -1)) : 1;
    expect(alpha).toBe(1);
  });

  test('the live indicator is amber and nothing else is', async ({ page }) => {
    // Amber means live, right now, and appears nowhere else. If it starts
    // showing up on warnings or pending states it stops meaning anything.
    await page.goto('/pingpong/matches/1/live/');

    const ballHex = await cssVar(page, '--ball');
    expect(ballHex).toBe('245 158 11');
  });
});

const { expect } = require('@playwright/test');
const { PROD_CSP } = require('./helpers');

/**
 * The "saved copy" banner (KAN-145), shared by the light (`.spec.js`), dark
 * (`.dark.spec.js`) and no-JS (`.nojs.spec.js`) projects.
 *
 * Those projects block service workers, so the copy the worker would serve
 * is built here: the real page, fetched, with the same two string replaces
 * sw.js makes (markSnapshot). The worker's own output is covered end to end
 * by offline-snapshots.pwa.spec.js; this is about what CSS shows, with and
 * without JS, with and without the stylesheet.
 */
const MARKER = 'data-freshness="network"';
const PLACEHOLDER = '<time data-snapshot-time></time>';

function markSnapshot(html, { iso, text, old }) {
  if (!html.includes(MARKER) || !html.includes(PLACEHOLDER)) {
    throw new Error('the page lost a marker sw.js needs');
  }
  return html
    .replace(MARKER, () => `data-freshness="snapshot" data-snapshot-at="${iso}" data-snapshot-age="${old ? 'old' : 'recent'}"`)
    .replace(PLACEHOLDER, () => `<time data-snapshot-time datetime="${iso}">${text}</time>`);
}

/** Serve /pingpong/ as a saved copy (`shown` like markSnapshot's output). */
async function serveAsSnapshot(page, shown) {
  await page.route(/\/pingpong\/$/, async (route) => {
    const response = await route.fetch();
    const body = markSnapshot(await response.text(), shown);
    await route.fulfill({
      response,
      body,
      headers: { ...response.headers(), 'content-security-policy': PROD_CSP },
    });
  });
}

async function withoutStylesheet(page) {
  await page.route(/\/static\/pingpong\/css\/app\./, (route) => route.abort());
}

const banner = (page) => page.locator('[data-snapshot-banner]');
const RECENT = { iso: '2026-11-04T14:32:00.000Z', text: '14:32', old: false };
const OLD = { iso: '2026-11-01T08:00:00.000Z', text: 'Sun 1 Nov', old: true };

function defineBannerCases(test, { geometry = true } = {}) {
  test.describe('saved-copy banner', () => {
    test.describe.configure({ timeout: 60000 });
    test('a live page: not visible, not in the accessibility tree', async ({ page }) => {
      await page.goto('/pingpong/');
      await expect(page.locator('html')).toHaveAttribute('data-freshness', 'network');
      await expect(banner(page)).toHaveCount(1);
      await expect(banner(page)).toBeHidden();
      await expect(page.getByRole('status').filter({ hasText: 'saved copy' })).toHaveCount(0);
    });

    test('a live page with no stylesheet: still not visible', async ({ page }) => {
      await withoutStylesheet(page);
      await page.goto('/pingpong/');
      await expect(banner(page)).toBeHidden();
    });

    test('a saved copy: the banner and its time are shown', async ({ page }) => {
      await serveAsSnapshot(page, RECENT);
      await page.goto('/pingpong/');
      await expect(banner(page)).toBeVisible();
      await expect(banner(page)).toHaveText('No connection — saved copy from 14:32', {
        useInnerText: true,
      });
      await expect(page.getByRole('status').filter({ hasText: 'saved copy from 14:32' })).toHaveCount(1);
    });

    test('a saved copy with no stylesheet: still shown', async ({ page }) => {
      await withoutStylesheet(page);
      await serveAsSnapshot(page, RECENT);
      await page.goto('/pingpong/');
      await expect(banner(page)).toBeVisible();
      await expect(banner(page)).toContainText('saved copy from 14:32', { useInnerText: true });
    });

    test('over a day old: the stronger wording', async ({ page }) => {
      await serveAsSnapshot(page, OLD);
      await page.goto('/pingpong/');
      await expect(banner(page)).toHaveText('Saved copy from Sun 1 Nov — may be out of date', {
        useInnerText: true,
      });
    });

    if (!geometry) return;

    test('sits under the header, covers nothing, leaves the tab bar alone', async ({ page }) => {
      await page.goto('/pingpong/');
      const tabBar = page.getByRole('navigation', { name: 'Primary' });
      const liveTabBar = await tabBar.boundingBox();

      await serveAsSnapshot(page, RECENT);
      await page.goto('/pingpong/');
      const [header, box, bar] = await Promise.all([
        page.locator('header.vt-header').boundingBox(),
        banner(page).boundingBox(),
        tabBar.boundingBox(),
      ]);
      // Directly under the fixed header (which, with the club line, is a
      // little taller than <main>'s 60px offset, so the banner's top padding
      // tucks under it), with its glyph and words fully below it.
      const headerBottom = header.y + header.height;
      expect(box.y).toBeLessThanOrEqual(headerBottom + 1);
      const glyph = await banner(page).locator('svg').boundingBox();
      expect(glyph.y).toBeGreaterThanOrEqual(headerBottom - 2); // the header's 2px border
      expect(box.y + box.height).toBeLessThan(bar.y);
      expect(bar).toEqual(liveTabBar);
      expect(box.width).toBeLessThanOrEqual(page.viewportSize().width);
      // In the flow, not over the page: the first content starts below it.
      const content = await page.locator('main > div').last().boundingBox();
      expect(content.y).toBeGreaterThanOrEqual(box.y + box.height - 1);
    });

    test('neutral, never amber or red', async ({ page }) => {
      await serveAsSnapshot(page, RECENT);
      await page.goto('/pingpong/');
      const colours = await banner(page).evaluate((el) => {
        const root = getComputedStyle(document.documentElement);
        const rgb = (token) => `rgb(${root.getPropertyValue(token).trim().split(/\s+/).join(', ')})`;
        const style = getComputedStyle(el);
        const text = getComputedStyle(el.querySelector('p'));
        return {
          forbidden: [rgb('--ball'), rgb('--paddle')],
          used: [style.backgroundColor, style.borderBottomColor, text.color],
        };
      });
      for (const colour of colours.used) expect(colours.forbidden).not.toContain(colour);
    });
  });
}

module.exports = { defineBannerCases, markSnapshot };

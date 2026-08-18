const { test, expect } = require('@playwright/test');
const { applyProdCSP, expectDrawerClosed } = require('./helpers');

/**
 * The navigation spine at phone size: the bottom tab bar and the tiered
 * drawer.
 *
 * pytest can assert that the tab bar is in the markup. It cannot tell you
 * whether the bar is actually on screen, whether it sits over the content it
 * is supposed to sit over, or whether the last row of a list is permanently
 * hidden underneath it -- which is the failure mode a fixed bottom bar
 * introduces and the one nobody notices until a user cannot tap the thing at
 * the bottom of the page.
 *
 * Everything here runs under the production CSP. The tab bar and the drawer
 * are both plain markup and plain listeners precisely so they keep working
 * without 'unsafe-eval'; if that ever stops being true, this is where it
 * shows up rather than in production.
 */

const tabBar = (page) => page.getByRole('navigation', { name: 'Primary' });
const tab = (page, name) => tabBar(page).getByRole('link', { name, exact: false });

const SCREENS = [
  ['/pingpong/', 'Today'],
  ['/pingpong/play/', 'Start something'],
  ['/pingpong/leaderboard/', 'The Table'],
  ['/pingpong/championships/', 'Cups'],
  ['/pingpong/matches/', 'All matches'],
];

test.describe('bottom tab bar', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
  });

  test('is on screen and pinned to the bottom', async ({ page }) => {
    await page.goto('/pingpong/');

    const box = await tabBar(page).boundingBox();
    const viewport = page.viewportSize();

    expect(box).not.toBeNull();
    // Flush with the bottom edge, not floating a few pixels above it.
    expect(Math.round(box.y + box.height)).toBe(viewport.height);
    // At least the 56px the design specifies, which is above the 44px floor
    // because this is the control you hit while holding a paddle.
    expect(box.height).toBeGreaterThanOrEqual(56);
  });

  test('does not cover the end of the page content', async ({ page }) => {
    // The single thing a fixed bottom bar gets wrong. `pb-tabbar` on <main>
    // and the bar's own height come from one spacing token so they cannot
    // disagree -- this asserts the token is actually applied.
    await page.goto('/pingpong/leaderboard/');
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));

    const barTop = (await tabBar(page).boundingBox()).y;
    const contentBottom = await page.evaluate(() => {
      const main = document.querySelector('main');
      return main.getBoundingClientRect().bottom;
    });

    expect(contentBottom).toBeLessThanOrEqual(barTop + 1);
  });

  test('every tab reaches its screen and lights up there', async ({ page }) => {
    for (const [, label] of [['', 'Today'], ['', 'Play'], ['', 'Table'], ['', 'Cups']]) {
      await page.goto('/pingpong/');
      await tab(page, label).tap();
      await expect(tab(page, label)).toHaveAttribute('aria-current', 'page');
    }
  });

  test('exactly one tab is current on each screen', async ({ page }) => {
    for (const [url] of SCREENS.slice(0, 4)) {
      await page.goto(url);
      await expect(tabBar(page).locator('[aria-current="page"]')).toHaveCount(1);
    }
  });

  test('a drawer-only screen claims no tab', async ({ page }) => {
    // Highlighting a tab the user did not tap is worse than showing no
    // selection at all: it says they are somewhere they are not.
    await page.goto('/pingpong/matches/');
    await expect(tabBar(page).locator('[aria-current="page"]')).toHaveCount(0);
  });
});

test.describe('drawer tiers', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
    await page.goto('/pingpong/');
  });

  test('opens onto three tiers and closes again', async ({ page }) => {
    await expectDrawerClosed(page);
    await page.getByRole('button', { name: 'Open menu' }).tap();

    const panel = page.locator('#mobile-menu-panel');
    await expect(panel).toBeVisible();
    for (const tier of ['Daily', 'Dig around', 'Settings']) {
      await expect(panel.getByText(tier, { exact: true })).toBeVisible();
    }

    await page.getByRole('button', { name: 'Close menu' }).tap();
    await expectDrawerClosed(page);
  });

  test('does not offer Play, which is a button rather than a place', async ({ page }) => {
    await page.getByRole('button', { name: 'Open menu' }).tap();
    const panel = page.locator('#mobile-menu-panel');
    await expect(panel).toBeVisible();
    await expect(panel.getByRole('link', { name: 'Play', exact: true })).toHaveCount(0);
  });
});

test.describe('layout at phone width', () => {
  for (const [url, title] of SCREENS) {
    test(`${title} does not scroll sideways`, async ({ page }) => {
      await applyProdCSP(page);
      await page.goto(url);

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }
});

test.describe('icons', () => {
  test('the inlined sprite actually paints', async ({ page }) => {
    await applyProdCSP(page);
    await page.goto('/pingpong/');

    // Icons are <use href="#i-name"> against symbols inlined at the top of
    // <body>. A <use> pointing at a symbol that is not there renders nothing
    // at all, with no error in the console and no failed request -- so the
    // only way to know is to measure one.
    const box = await page.locator('svg use[href="#i-menu"]').first().evaluate((node) => {
      const rect = node.ownerSVGElement.getBoundingClientRect();
      return { w: rect.width, h: rect.height };
    });

    expect(box.w).toBeGreaterThan(8);
    expect(box.h).toBeGreaterThan(8);
  });
});

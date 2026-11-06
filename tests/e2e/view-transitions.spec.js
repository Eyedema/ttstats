const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const {
  recordPageReveal,
  pageReveal,
  renderedTransitionNames,
  transitionNameOf,
} = require('./view-transitions');

/**
 * Cross-document view transitions on a phone (KAN-125).
 *
 * WebKit supports them (Safari 18.2+), so this is the real thing: a tab tap
 * cross-fades the content while the header and tab bar stay put. The
 * scoreboard opts out entirely -- nothing may sit between a finger and a
 * point being scored.
 */

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
  await recordPageReveal(page);
});

// applyProdCSP proxies every request; a deferred script still in flight when
// the test ends would otherwise fail it with "route.fetch: Test ended".
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

function tab(page, name) {
  return page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name });
}

test('a tab-to-tab navigation is revealed through a view transition', async ({ page }) => {
  await page.goto('/pingpong/');
  await tab(page, 'Table').tap();
  await expect(page).toHaveURL(/\/pingpong\/leaderboard\/$/);
  await expect(tab(page, 'Table')).toHaveAttribute('aria-current', 'page');

  expect((await pageReveal(page)).revealed).toBe(true);
});

test('the chrome is named so it stays put, and every name is unique', async ({ page }) => {
  await page.goto('/pingpong/');

  expect(await transitionNameOf(page, 'header.vt-header')).toBe('app-header');
  expect(await transitionNameOf(page, 'nav[aria-label="Primary"]')).toBe('tab-bar');
  expect(await transitionNameOf(page, 'header .vt-title')).toBe('page-title');
  expect(await transitionNameOf(page, '#nav-progress')).toBe('nav-progress');

  // A duplicate name aborts the whole transition, so this is a correctness
  // check, not tidiness. The sidebar is display:none here and not captured.
  const names = await renderedTransitionNames(page);
  expect(names).toEqual(expect.arrayContaining(['app-header', 'tab-bar', 'page-title']));
  expect(names).not.toContain('app-sidebar');
  expect(new Set(names).size).toBe(names.length);
});

test('an open drawer keeps a unique name too', async ({ page }) => {
  await page.goto('/pingpong/');
  await page.locator('#mobile-menu-open').tap();
  await expect(page.locator('#mobile-menu-scrim')).toHaveClass(/is-open/);

  const names = await renderedTransitionNames(page);
  expect(names).toContain('app-drawer');
  expect(new Set(names).size).toBe(names.length);
});

test.describe('the scoreboard', () => {
  async function scoreboardUrl(page) {
    await page.goto('/pingpong/');
    return page.getByRole('link', { name: /Resume/i }).first().getAttribute('href');
  }

  test('is entered and left with no transition and names nothing', async ({ page }) => {
    const url = await scoreboardUrl(page);

    // Control: the same kind of navigation between two ordinary pages does
    // transition, so the `false` below is the opt-out and not the method.
    await page.evaluate(() => { location.href = '/pingpong/leaderboard/'; });
    await expect(page).toHaveURL(/\/pingpong\/leaderboard\/$/);
    expect((await pageReveal(page)).revealed).toBe(true);

    await page.evaluate((u) => { location.href = u; }, url);
    await expect(page.locator('#scoreboard')).toBeVisible();
    expect((await pageReveal(page)).revealed).toBe(false);

    const named = await page.evaluate(() =>
      [...document.querySelectorAll('*')]
        .map((el) => getComputedStyle(el).viewTransitionName)
        .filter((name) => name && name !== 'none')
    );
    expect(named).toEqual([]);

    await page.evaluate(() => { location.href = '/pingpong/'; });
    await expect(page).toHaveURL(/\/pingpong\/$/);
    expect((await pageReveal(page)).revealed).toBe(false);
  });
});

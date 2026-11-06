const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const {
  recordPageReveal,
  pageReveal,
  renderedTransitionNames,
  transitionNameOf,
} = require('./view-transitions');

/**
 * Cross-document view transitions on Android (KAN-125).
 *
 * Chrome for Android has them (126+): same expectations as Safari. Firefox
 * does not (cross-document VT is Nightly-only as of Firefox 147; same-document
 * `startViewTransition` shipped in 144), so there the contract is "exactly as
 * before": navigation works, nothing errors, no transition, no layout shift.
 */

const DARK_COURT = 'rgb(11, 18, 32)';

// page.route (applyProdCSP) skips requests from a worker-controlled page.
test.use({ serviceWorkers: 'block' });

function engineHasCrossDocumentVT(projectName) {
  return projectName === 'android-chrome';
}

function tab(page, name) {
  return page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name });
}

/** Errors the page raised: uncaught exceptions and console.error. */
function collectErrors(page) {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text());
  });
  return errors;
}

/** Header and tab bar boxes: the chrome must not move between pages. */
async function chromeBoxes(page) {
  return {
    header: await page.locator('header.vt-header').boundingBox(),
    tabBar: await page.getByRole('navigation', { name: 'Primary' }).boundingBox(),
  };
}

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
  await recordPageReveal(page);
});

// applyProdCSP proxies every request; a deferred script still in flight when
// the test ends would otherwise fail it with "route.fetch: Test ended".
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('tab to tab: transition where supported, today\'s behaviour elsewhere', async ({ page }, info) => {
  const errors = collectErrors(page);
  await page.goto('/pingpong/');
  const before = await chromeBoxes(page);

  await tab(page, 'Table').tap();
  await expect(page).toHaveURL(/\/pingpong\/leaderboard\/$/);
  await expect(tab(page, 'Table')).toHaveAttribute('aria-current', 'page');

  const reveal = await pageReveal(page);
  if (engineHasCrossDocumentVT(info.project.name)) {
    expect(reveal.revealed).toBe(true);
  } else {
    expect(reveal.revealed).not.toBe(true);
  }

  expect(await chromeBoxes(page)).toEqual(before);
  expect(errors).toEqual([]);
});

test('the chrome carries unique transition names', async ({ page }) => {
  await page.goto('/pingpong/');
  // view-transition-name parses in Firefox too (it has same-document VT);
  // it is inert there without a transition, and changes no layout.
  expect(await transitionNameOf(page, 'header.vt-header')).toBe('app-header');
  expect(await transitionNameOf(page, 'nav[aria-label="Primary"]')).toBe('tab-bar');
  const names = await renderedTransitionNames(page);
  expect(new Set(names).size).toBe(names.length);
});

test.describe('dark', () => {
  test.use({ colorScheme: 'dark' });

  test('the transition shows the court behind the snapshots, never white', async ({ page }, info) => {
    test.skip(!engineHasCrossDocumentVT(info.project.name), 'no cross-document VT in this engine');
    await page.goto('/pingpong/');
    await tab(page, 'Cups').tap();
    await expect(page).toHaveURL(/\/championships\/$/);

    const reveal = await pageReveal(page);
    expect(reveal.revealed).toBe(true);
    expect(reveal.background).toBe(DARK_COURT);
  });
});

test.describe('reduced motion', () => {
  test.beforeEach(async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
  });

  test('no transition at all', async ({ page }) => {
    await page.goto('/pingpong/');
    expect(
      await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)
    ).toBe(true);

    await tab(page, 'Table').tap();
    await expect(page).toHaveURL(/\/pingpong\/leaderboard\/$/);
    expect((await pageReveal(page)).revealed).not.toBe(true);
  });
});

const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');
const { emulateStandalone, pull, touch, watchForReload, indicator } = require('./pull-to-refresh-helpers');

/**
 * Pull to refresh with Reduce Motion on (KAN-126): the indicator does not
 * travel, turn or spin. It appears in place and only its text changes.
 * Workers blocked so applyProdCSP's page.route() applies.
 */
test.use({ serviceWorkers: 'block' });

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await emulateStandalone(page);
});

test('the preference actually reached the page', async ({ page }) => {
  await page.goto('/pingpong/');
  expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
});

test('the indicator states its state in text and does not move', async ({ page }) => {
  await page.goto('/pingpong/');

  await touch(page, 'touchstart', 0);
  await touch(page, 'touchmove', 40);
  await touch(page, 'touchmove', 140);

  const ptr = indicator(page);
  await expect(ptr).toHaveText('Release to refresh');
  await expect(ptr).toHaveClass(/is-visible/);
  await expect(ptr).toHaveClass(/is-static/);
  const motion = await ptr.evaluate((el) => ({
    y: el.style.getPropertyValue('--ptr-y'),
    r: el.style.getPropertyValue('--ptr-r'),
    icon: getComputedStyle(el.querySelector('svg')).transform,
  }));
  expect(motion.y).toBe('');
  expect(motion.r).toBe('');
  // rotate(0deg) computes to the identity matrix; anything else has turned.
  expect(['none', 'matrix(1, 0, 0, 1, 0, 0)']).toContain(motion.icon);

  await touch(page, 'touchcancel', 140);
  await expect(ptr).not.toHaveClass(/is-visible/);
});

test('offline, it says so without spinning', async ({ page }) => {
  await page.goto('/pingpong/');
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false });
  });
  const reload = await watchForReload(page);

  await pull(page, { dy: 200 });

  await expect(indicator(page)).toHaveText('No connection');
  await expect(indicator(page)).not.toHaveClass(/is-refreshing/);
  await reload.didNotHappen();
});

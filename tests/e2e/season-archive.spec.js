const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP, expectDrawerClosed } = require('./helpers');

/**
 * The season archive (KAN-51 / S4.6): a past season's frozen final table.
 *
 * Runs as the `e2e-seasons` user (the one with season results).
 * seed_e2e seeds an archived season,
 * "E2E Spring 2025", whose singles table has a shared first place and a
 * deleted player. The page has no JS of its own, so what matters here is that
 * it renders at phone width under the production CSP, that the shared rank
 * and the frozen name survive the real stylesheet, and that nothing throws.
 */

test.use({ storageState: path.join(__dirname, '.auth', 'seasons.json') });

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

function collectPageErrors(page) {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  return errors;
}

test('the archive lists the season and opens its frozen table', async ({ page }) => {
  const errors = collectPageErrors(page);

  await page.goto('/pingpong/seasons/');
  const main = page.locator('main');
  await expect(main.getByRole('heading', { name: 'Past seasons' })).toBeVisible();
  await expectDrawerClosed(page);

  const season = main.getByRole('link', { name: /E2E Spring 2025/ });
  await expect(season).toBeVisible();
  await season.tap();

  await expect(page).toHaveURL(/\/pingpong\/seasons\/\d+\/$/);
  await expect(main.getByRole('heading', { name: 'E2E Spring 2025' })).toBeVisible();

  const table = main.getByRole('list', { name: 'Singles final table' });
  await expect(table).toBeVisible();
  const ranks = table.getByTestId('standing-rank');
  await expect(ranks).toHaveText(['1', '1', '3']);

  // A deleted player still reads by the name they finished under, as text.
  const departed = table.getByText('E2E Departed');
  await expect(departed).toBeVisible();
  expect(await departed.evaluate((el) => el.closest('a'))).toBeNull();

  // The rows fit a phone: no horizontal scroll at iPhone width.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);

  expect(errors).toEqual([]);
});

test('the format switch shows the doubles table', async ({ page }) => {
  const errors = collectPageErrors(page);

  await page.goto('/pingpong/seasons/');
  await page.locator('main').getByRole('link', { name: /E2E Spring 2025/ }).tap();

  const formats = page.getByRole('navigation', { name: 'Format' });
  await expect(formats.getByRole('link', { name: 'Singles' })).toHaveAttribute('aria-current', 'page');
  await formats.getByRole('link', { name: 'Doubles' }).tap();

  await expect(page).toHaveURL(/format=doubles/);
  await expect(formats.getByRole('link', { name: 'Doubles' })).toHaveAttribute('aria-current', 'page');
  const table = page.locator('main').getByRole('list', { name: 'Doubles final table' });
  await expect(table.getByTestId('standing-row')).toHaveCount(1);
  await expect(table.getByTestId('standing-rating')).toHaveText(['1516']);

  expect(errors).toEqual([]);
});

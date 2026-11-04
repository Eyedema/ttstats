const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The seasons leaderboard (KAN-48 / S4.3) and the provisional marker
 * (KAN-50 / S4.4), as the `e2e-seasons` user, who has the `seasons` flag on by
 * override (`seed_e2e`). Every other spec runs as `e2e`, flag off.
 *
 * pytest proves the context and the markup. It cannot prove the filters are
 * wired: a dead Period control has shipped on this page before -- an invalid
 * `hx-trigger` selector, which also raised a pageerror on every load -- so
 * this drives the real selects under the production CSP and asserts that
 * nothing on the page errors.
 */
test.use({ storageState: path.join(__dirname, '.auth', 'seasons.json') });

const BALL = 'rgb(245, 158, 11)';

test.describe('season leaderboard', () => {
  let errors;

  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(`console: ${m.text()}`);
    });
    await applyProdCSP(page);
    await page.goto('/pingpong/leaderboard/');
  });

  test.afterEach(() => {
    expect(errors).toEqual([]);
  });

  test('defaults to the current season, singles', async ({ page }) => {
    const results = page.locator('#leaderboard-results');
    await expect(results.locator('[data-scope-label]')).toContainText('Singles · E2E Season');
    await expect(results.getByRole('link', { name: 'E2E Seasons Player' })).toBeVisible();
    await expect(page.locator('#id_season')).toHaveValue(/^\d+$/);
  });

  test('switching season and format refetches the table', async ({ page }) => {
    const results = page.locator('#leaderboard-results');

    let responded = page.waitForResponse(
      (r) => r.url().includes('/pingpong/leaderboard/') && r.url().includes('season=all'),
    );
    await page.locator('#id_season').selectOption('all');
    await responded;
    await expect(page).toHaveURL(/season=all/);
    await expect(results.locator('[data-scope-label]')).toContainText('Singles · all-time');
    // Rated all-time from the momentum match: on the all-time table, not unrated.
    await expect(results.locator('[data-rated-row]', { hasText: 'E2E Momentum' })).toBeVisible();

    responded = page.waitForResponse(
      (r) => r.url().includes('/pingpong/leaderboard/') && r.url().includes('format=doubles'),
    );
    await page.locator('#id_format').selectOption('doubles');
    await responded;
    await expect(page).toHaveURL(/format=doubles/);
    await expect(results.locator('[data-scope-label]')).toContainText('Doubles · all-time');
    await expect(results.getByText('Nobody is rated here yet')).toBeVisible();

    // A second swap after the first: outerHTML swaps die silently if the
    // fragment loses its own id.
    responded = page.waitForResponse(
      (r) => r.url().includes('/pingpong/leaderboard/') && r.url().includes('format=singles'),
    );
    await page.locator('#id_format').selectOption('singles');
    await responded;
    await expect(results.locator('[data-scope-label]')).toContainText('Singles · all-time');
  });

  test('a player with no match this season is unrated, not 1500', async ({ page }) => {
    const unrated = page.locator('[data-unrated-list] li', { hasText: 'E2E Momentum' });
    await expect(unrated).toBeVisible();
    await expect(unrated.locator('[data-rating-status="unrated"]')).toHaveText(/unrated/i);
    await expect(unrated).not.toContainText(/\d{3,4}/);
  });

  test('the provisional marker is a word, not amber, and explains itself', async ({ page }) => {
    const marker = page.locator('#leaderboard-results [data-rating-status="provisional"]').first();
    await expect(marker).toBeVisible();
    await expect(marker).toHaveText(/provisional/i);
    const color = await marker.evaluate((el) => getComputedStyle(el).color);
    expect(color).not.toBe(BALL);

    // The marker fits beside a name at 390px rather than overflowing the row.
    const row = page.locator('[data-rated-row]').first();
    const [rowBox, markerBox] = [await row.boundingBox(), await marker.boundingBox()];
    expect(markerBox.x + markerBox.width).toBeLessThanOrEqual(rowBox.x + rowBox.width);

    // The explanation is a native <details>: closed until asked.
    const help = page.locator('#rating-help');
    await expect(help).not.toHaveAttribute('open', '');
    await help.locator('summary').tap();
    await expect(help).toHaveAttribute('open', '');
    await expect(help).toContainText('rated matches in that format');
    await expect(help).toContainText('Singles and doubles are rated separately');
  });

  test('the profile shows each format with its state', async ({ page }) => {
    await page.locator('#leaderboard-results').getByRole('link', { name: 'E2E Seasons Player' }).tap();
    await page.waitForURL(/\/players\/\d+\//);
    const ratings = page.locator('[data-format-ratings]');
    await expect(ratings).toContainText('Singles');
    await expect(ratings.locator('[data-rating-status="provisional"]').first()).toBeVisible();
    // Never played doubles: unrated in both columns.
    await expect(ratings.locator('[data-rating-status="unrated"]')).toHaveCount(2);
  });
});

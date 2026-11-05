const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The Glicko-2 rating UI (KAN-71 / KAN-72), as the `e2e-glicko` user, who is
 * rated and has a result awaiting their confirmation (`seed_e2e`). Since
 * KAN-73 Glicko-2 is the only system; "no Elo anywhere" is asserted here.
 *
 * pytest proves the markup of every surface. It cannot prove the htmx switch
 * stays Glicko, that the chart canvas actually paints, or that the page works
 * under the production CSP, so those are driven here.
 */
test.use({ storageState: path.join(__dirname, '.auth', 'glicko.json') });

const ELO = /\bElo\b/;

async function playerHref(page, name) {
  await page.goto('/pingpong/players/');
  return page
    .locator('tr')
    .filter({ hasText: name })
    .first()
    .locator('a[href^="/pingpong/players/"]:not([href*="edit"])')
    .first()
    .getAttribute('href');
}

test.describe('glicko-2 cutover', () => {
  let errors;

  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    await applyProdCSP(page);
  });

  test.afterEach(() => {
    expect(errors).toEqual([]);
  });

  test('the leaderboard shows the uncertainty and the provisional marker', async ({ page }) => {
    await page.goto('/pingpong/leaderboard/');
    const results = page.locator('#leaderboard-results');
    await expect(results).toHaveAttribute('data-rating-system', 'glicko2');

    const row = results.locator('[data-rated-row]', { hasText: 'E2E Glicko Player' });
    await expect(row).toBeVisible();
    await expect(row.locator('[data-rating-rd]')).toHaveText(/^±\d+$/);
    await expect(row.locator('[data-rating-status="provisional"]')).toHaveText(/provisional/i);
    expect(await page.locator('main').innerText()).not.toMatch(ELO);
  });

  test('the season and format switch stays Glicko', async ({ page }) => {
    await page.goto('/pingpong/leaderboard/');
    const results = page.locator('#leaderboard-results');

    // Wait for the htmx swap itself (the partial carries its scope), not just
    // the response, then assert the system and the +/- on the NEW content.
    await page.locator('#id_season').selectOption('all');
    await expect(results).toHaveAttribute('data-scope', /^all-singles$/);
    await expect(results).toHaveAttribute('data-rating-system', 'glicko2');
    await expect(results.locator('[data-rating-rd]').first()).toHaveText(/^±\d+$/);

    await page.locator('#id_format').selectOption('doubles');
    await expect(results).toHaveAttribute('data-scope', /^all-doubles$/);
    await expect(results).toHaveAttribute('data-rating-system', 'glicko2');
    expect(await page.locator('main').innerText()).not.toMatch(ELO);
  });

  test('the pending delta is the same on the dashboard and the match page', async ({ page }) => {
    await page.goto('/pingpong/');
    const waiting = page.locator('main [data-rating-delta]').first();
    await expect(waiting).toBeVisible();
    const onDashboard = (await waiting.innerText()).trim();
    expect(onDashboard).toMatch(/^[+-]\d+$/);

    const href = await waiting
      .locator('xpath=ancestor::a[1]')
      .getAttribute('href');
    await page.goto(href);

    // Both sides' previews are shown; mine is the one that equals the dashboard's.
    const deltas = page.locator('main [data-rating-delta]');
    await expect(deltas.first()).toContainText('if agreed');
    const texts = (await deltas.allInnerTexts()).map((t) => t.trim());
    expect(texts.some((t) => t === `${onDashboard} if agreed`)).toBe(true);
    expect(await page.locator('main').innerText()).not.toMatch(ELO);
  });

  test('the player chart draws a band under the production CSP', async ({ page }) => {
    await page.goto(await playerHref(page, 'E2E Glicko Player'));

    const canvas = page.locator('#ratingChart');
    await expect(canvas).toBeVisible();
    await expect(page.locator('[data-rating-card]').first()).toContainText('±');
    // Chart.js has painted when the canvas has non-transparent pixels.
    await expect
      .poll(() =>
        canvas.evaluate((c) => {
          const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          for (let i = 3; i < data.length; i += 4) if (data[i] !== 0) return true;
          return false;
        }),
      )
      .toBe(true);
    expect(await page.locator('main').innerText()).not.toMatch(ELO);
  });

  test('the head to head prediction is in the viewer system', async ({ page }) => {
    const one = (await playerHref(page, 'E2E Glicko Player')).match(/players\/(\d+)\//)[1];
    const two = (await playerHref(page, 'E2E Opponent')).match(/players\/(\d+)\//)[1];
    await page.goto(`/pingpong/head-to-head/?player1=${one}&player2=${two}`);

    const prediction = page.locator('[data-prediction]');
    await expect(prediction).toBeVisible();
    await expect(prediction).toHaveAttribute('data-rating-system', 'glicko2');
    await expect(prediction).toContainText(/\d+%/);
    await expect(prediction).not.toContainText(ELO);
  });

  test('the nav shows the rating with its uncertainty', async ({ page }) => {
    await page.goto('/pingpong/leaderboard/');
    const nav = page.locator('[data-nav-rating]').first();
    await expect(nav).toHaveAttribute('data-rating-system', 'glicko2');
    expect(await nav.evaluate((el) => el.textContent)).toMatch(/\d{4}\s*±\d+/);
  });

  test('the help link opens the explainer', async ({ page }) => {
    await page.goto('/pingpong/leaderboard/');
    // One link, inside the help details: open them first.
    await page.locator('#rating-help > summary').tap();
    const link = page.locator('#leaderboard-results [data-how-ratings-link]');
    await expect(link).toHaveCount(1);
    await link.tap();
    await expect(page).toHaveURL(/\/pingpong\/ratings\/$/);
    await expect(page.getByRole('heading', { name: 'How ratings work' })).toBeVisible();
    await expect(page.locator('main')).toContainText('give or take');
    expect(await page.locator('main').innerText()).not.toMatch(ELO);
  });
});

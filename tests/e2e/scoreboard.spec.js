const { test, expect } = require('@playwright/test');

/**
 * The live scoreboard on a real phone-sized WebKit viewport with touch.
 *
 * The Chrome-extension pass covered the optimistic-scoring logic but ran at a
 * fixed 1512px desktop viewport with mouse clicks. Neither of those is how
 * this screen is used.
 *
 * These specs share one live match and never assume it starts at 0-0: the
 * match carries state across tests, and a suite that assumes a fresh fixture
 * passes once and then fails for reasons unrelated to the code.
 */

const team1Score = (page) => page.locator('[x-ref="team1Score"]');
const team1Zone = (page) => page.locator('.tap-zone').first();

async function currentScore(page) {
  return Number(await team1Score(page).innerText());
}

test.describe('live scoreboard', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/pingpong/');
    const resume = page.getByRole('link', { name: /Resume/i }).first();
    await resume.tap();

    // The server picker only appears until someone has been chosen, so this
    // copes with both a fresh match and one already under way. Wait for Alpine
    // to have committed to one branch before asking which it picked -- probing
    // isVisible() mid-navigation just returns false and silently skips the
    // picker, leaving every later assertion staring at a hidden scoreboard.
    const picker = page.getByRole('heading', { name: /serving/i });
    await expect(picker.or(team1Score(page)).first()).toBeVisible();

    if (await picker.isVisible()) {
      await page.getByRole('button', { name: 'E2E Player' }).tap();
    }
    await expect(team1Score(page)).toBeVisible();
  });

  test('the giant numerals fit the viewport', async ({ page }) => {
    // The score-hero specimen is 150px per column on a 390px-wide screen split
    // in two. Enormous type is the point of this screen, so the guard that it
    // does not push the page sideways has to be a real measurement.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test('a tap moves the score before the request settles', async ({ page }) => {
    // Delay the POST by 1.5s, then require the numeral to have moved within
    // 500ms. The margin is the assertion: the score cannot have come from the
    // response, so it came from the optimistic overlay. This is the entire
    // point of the change and cannot be asserted from Python, which never
    // runs the JS.
    const SERVER_DELAY = 1500;
    await page.route('**/live/point/', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, SERVER_DELAY));
      await route.continue();
    });

    const before = await currentScore(page);
    await team1Zone(page).tap();
    await expect(team1Score(page)).toHaveText(String(before + 1), { timeout: 500 });
  });

  test('rapid taps are not dropped', async ({ page }) => {
    const before = await currentScore(page);
    const zone = team1Zone(page);
    for (let i = 0; i < 4; i += 1) await zone.tap();

    await expect(team1Score(page)).toHaveText(String(before + 4));

    // And the server agrees once the queue drains -- the check that the
    // optimistic overlay did not quietly diverge from the truth.
    await page.waitForTimeout(1000);
    await page.reload();
    await expect(team1Score(page)).toHaveText(String(before + 4));
  });

  test('a failed point rolls back and reports', async ({ page }) => {
    const before = await currentScore(page);
    await page.route('**/live/point/', (route) => route.abort('failed'));

    await team1Zone(page).tap();
    await expect(page.getByRole('alert')).toContainText(/offline/i);
    await expect(team1Score(page)).toHaveText(String(before));

    await page.unroute('**/live/point/');
  });

  test('the error toast does not collide with the team labels', async ({ page }) => {
    await page.route('**/live/point/', (route) => route.abort('failed'));
    await team1Zone(page).tap();

    const toast = page.getByRole('alert');
    await expect(toast).toBeVisible();
    const toastBox = await toast.boundingBox();
    // Scoped to the tap zone: the same player name also sits in the drawer and
    // the desktop sidebar, both hidden here, and a hidden node has no box.
    const labelBox = await team1Zone(page).getByText('E2E Player').boundingBox();

    // No vertical overlap with the top label band. This regressed once: the
    // toast was placed at top-4, straight through the names and game counts.
    expect(toastBox.y).toBeGreaterThan(labelBox.y + labelBox.height);

    await page.unroute('**/live/point/');
  });
});

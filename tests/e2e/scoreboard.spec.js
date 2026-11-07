const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The live scoreboard on a real phone-sized WebKit viewport with touch,
 * under the production CSP (KAN-95: the scoreboard is plain JS now, so it is
 * verified exactly as it ships -- no 'unsafe-eval').
 *
 * These specs share one live match and never assume it starts at 0-0: the
 * match carries state across tests, and a suite that assumes a fresh fixture
 * passes once and then fails for reasons unrelated to the code. The network
 * chaos scenarios (offline, lost acks, slow 4G) are scoreboard-offline-*.
 */

const team1Score = (page) => page.locator('[data-score="team1"]');
const team1Zone = (page) => page.locator('.tap-zone[data-side="team1"]');

async function currentScore(page) {
  return Number(await team1Score(page).innerText());
}

/** Everything tapped has reached the server. */
async function expectSynced(page) {
  await expect
    .poll(() => page.evaluate(() => document.getElementById('scoreboard').scoreboard.session.view().pending))
    .toBe(0);
}

test.describe('live scoreboard', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
    await page.goto('/pingpong/');
    // The seeded match is against E2E Opponent; the chaos specs use their own.
    const resume = page.locator('section', { hasText: 'E2E Opponent' })
      .getByRole('link', { name: /Resume/i }).first();
    await resume.tap();

    // The server picker only appears until someone has been chosen, so this
    // copes with both a fresh match and one already under way. Wait for the
    // page to have committed to one branch before asking which it picked.
    const picker = page.getByRole('heading', { name: /serving/i });
    await expect(picker.or(team1Score(page)).first()).toBeVisible();

    if (await picker.isVisible()) {
      await page.getByRole('button', { name: 'E2E Player' }).tap();
    }
    await expect(team1Score(page)).toBeVisible();
    await expectSynced(page);
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
    // Delay the sync by 1.5s, then require the numeral to have moved within
    // 500ms. The margin is the assertion: the score cannot have come from the
    // response, so it came from the local engine.
    const SERVER_DELAY = 1500;
    await page.route('**/live/sync/', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, SERVER_DELAY));
      await route.fallback();
    });

    const before = await currentScore(page);
    await team1Zone(page).tap();
    await expect(team1Score(page)).toHaveText(String(before + 1), { timeout: 500 });
    await expectSynced(page);
  });

  test('rapid taps are not dropped', async ({ page }) => {
    const before = await currentScore(page);
    const zone = team1Zone(page);
    for (let i = 0; i < 4; i += 1) await zone.tap();

    await expect(team1Score(page)).toHaveText(String(before + 4));

    // And the server agrees once the outbox drains.
    await expectSynced(page);
    await page.reload();
    await expect(team1Score(page)).toHaveText(String(before + 4));
  });

  test('a failed sync keeps the point and says it is saved on the phone', async ({ page }) => {
    // The pre-epic page dropped the point and toasted "Offline -- the score
    // has not been saved." Now the point stays, in the outbox, and is sent
    // when the network comes back.
    const before = await currentScore(page);
    await page.route('**/live/sync/', (route) => route.abort('failed'));

    await team1Zone(page).tap();
    const indicator = page.locator('[data-sync-indicator]');
    await expect(indicator).toContainText(/Offline .* saved on this phone/);
    await expect(team1Score(page)).toHaveText(String(before + 1));

    // Open the sheet while still failing (the backoff would otherwise retry
    // on its own and the indicator would vanish under the finger).
    await indicator.tap();
    const sheet = page.getByRole('dialog', { name: 'Saving the score' });
    await expect(sheet).toContainText('1 change is saved on this phone');
    await page.unroute('**/live/sync/');
    await sheet.getByRole('button', { name: 'Retry now' }).tap();
    await expectSynced(page);
    await expect(indicator).toBeHidden();
  });

  test('Undo takes the point back on the server, not just on screen', async ({ page }) => {
    // KAN-28. The persisted point log is unwound by the same op that rolls
    // the score back. The reload is the assertion that the server agreed.
    const before = await currentScore(page);
    await team1Zone(page).tap();
    await expect(team1Score(page)).toHaveText(String(before + 1));

    await page.getByRole('button', { name: 'Undo' }).tap();
    await expect(team1Score(page)).toHaveText(String(before));

    await expectSynced(page);
    await page.reload();
    await expect(team1Score(page)).toHaveText(String(before));
  });

  test('the error toast does not collide with the team labels', async ({ page }) => {
    // A server rejection is the toast's real trigger: rewrite the answer to
    // name the op as rejected.
    await page.route('**/live/sync/', async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const seq = JSON.parse(route.request().postData()).ops[0].seq;
      await route.fulfill({ response, json: { ...body, rejected: { seq, error: 'test rejection' } } });
    });
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

    await page.unroute('**/live/sync/');
  });

  test('nothing the page runs is refused by the production CSP', async ({ page }) => {
    const refused = [];
    page.on('console', (msg) => {
      if (/Content Security Policy|unsafe-eval|Refused to/i.test(msg.text())) refused.push(msg.text());
    });
    page.on('pageerror', (err) => refused.push(err.message));
    await page.reload();
    await team1Zone(page).tap();
    await expectSynced(page);
    // Alpine still boots on every page (base.html) and has nothing to do
    // here; anything else refused would be the scoreboard.
    expect(refused.filter((t) => !/Alpine|EvalError|Function\(\)/.test(t))).toEqual([]);
  });
});

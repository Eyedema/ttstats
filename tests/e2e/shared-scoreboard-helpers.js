const { expect } = require('@playwright/test');

/**
 * The seeded two-phone match (KAN-153, `seed_e2e`): E2E Player hosts on
 * side 1, E2E Partner is the guest on side 2, both restricted to their own
 * side. The suite only ever drives the host's phone (one login); the
 * two-context specs are KAN-155's. Specs share the match across projects,
 * so nothing assumes a score: they read it first.
 */

/** Open the seeded shared match's scoreboard from Today. */
async function openSharedScoreboard(page) {
  await page.goto('/pingpong/');
  await page.locator('section', { hasText: 'E2E Partner' })
    .getByTestId('live-resume-link').first().click();
  await expect(page.locator('#scoreboard')).toBeVisible();
}

/** Pick E2E Player to serve if nobody has yet; wait for the board. */
async function startIfNeeded(page) {
  const picker = page.getByRole('heading', { name: /serving/i });
  const board = page.locator('[data-score="team1"]');
  await expect(picker.or(board).first()).toBeVisible();
  if (await picker.isVisible()) {
    await page.getByRole('button', { name: 'E2E Player', exact: true }).tap();
  }
  await expect(board).toBeVisible();
  await expectSynced(page);
}

async function expectSynced(page) {
  await expect.poll(() => page.evaluate(
    () => document.getElementById('scoreboard').scoreboard.session.view().pending,
  ), { timeout: 15000 }).toBe(0);
}

/** Every GET /live/state/ the page sends, with its query. */
function recordStateRequests(page) {
  const seen = [];
  page.on('request', (req) => {
    const url = new URL(req.url());
    if (req.method() === 'GET' && /\/live\/state\/$/.test(url.pathname)) {
      seen.push({ since: url.searchParams.get('since'), wait: url.searchParams.get('wait') });
    }
  });
  return seen;
}

module.exports = { openSharedScoreboard, startIfNeeded, expectSynced, recordStateRequests };

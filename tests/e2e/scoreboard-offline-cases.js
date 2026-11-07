const { test, expect } = require('@playwright/test');
const {
  applyProdCSP, createLiveMatch, dropAcks, finishLiveMatch, goOffline, goOnline, releaseProdCSP,
  liveState, syncFromAnotherDevice, withLatency, E2E_USERNAME, E2E_PASSWORD,
} = require('./helpers');

/**
 * Network chaos for the live scoreboard (KAN-83, scenarios 1, 2, 3, 5, 6;
 * scenario 4 -- reload offline through the service worker -- is the
 * `iphone-pwa` project's). Shared by scoreboard-offline.spec.js (iphone-safari)
 * and scoreboard-offline.desktop.spec.js (desktop-chrome).
 *
 * Every test creates its own best-of-3 live match against "E2E Chaos" through
 * the real Score-live form and finishes it afterwards, so nothing it does is
 * visible to another spec. All under applyProdCSP: the chaos routes are
 * registered after it and fall back to it (helpers.js explains the order).
 * Randomness is seeded.
 */

const SYNC = '**/live/sync/';

const score = (page, side) => page.locator(`[data-score="${side}"]`);
const games = (page, side) => page.locator(`[data-games="${side}"]`);
const zone = (page, side) => page.locator(`.tap-zone[data-side="${side}"]`);
const indicator = (page) => page.locator('[data-sync-indicator]');

async function tap(page, side, times = 1) {
  // A real tap where the project has touch: WebKit folds rapid synthetic
  // mouse clicks on one spot into a double-click and drops some, which no
  // finger on a phone does.
  const touch = test.info().project.use.hasTouch;
  for (let i = 0; i < times; i += 1) {
    if (touch) await zone(page, side).tap();
    else await zone(page, side).click();
  }
}

async function sessionView(page) {
  return page.evaluate(() => {
    const v = document.getElementById('scoreboard').scoreboard.session.view();
    return { pending: v.pending, phase: v.phase, appliedSeq: v.appliedSeq };
  });
}

async function expectSynced(page, timeout = 15000) {
  await expect.poll(async () => (await sessionView(page)).pending, { timeout }).toBe(0);
}

async function expectScore(page, t1, t2) {
  await expect(score(page, 'team1')).toHaveText(String(t1));
  await expect(score(page, 'team2')).toHaveText(String(t2));
}

/** Open the scoreboard, pick E2E Player to serve, wait for the start op to sync. */
async function openScoreboard(page, match) {
  await page.goto(match.url);
  await page.getByRole('button', { name: 'E2E Player' }).click();
  await expect(score(page, 'team1')).toBeVisible();
  await expectSynced(page);
}

/** `n`: sync requests sent (a lost ack is one: it reached the server).
 *  `answered`: those the page got a response to. A request already in flight
 *  when the network returns can fail locally (WebKit: "internal error")
 *  without reaching the server; it carried nothing, and only `n` counts it. */
function countSyncs(page) {
  const counter = { n: 0, answered: 0 };
  const isSync = (req) => req.url().endsWith('/live/sync/') && req.method() === 'POST';
  page.on('request', (req) => { if (isSync(req)) counter.n += 1; });
  page.on('response', (resp) => { if (isSync(resp.request())) counter.answered += 1; });
  return counter;
}

function defineOfflineCases() {
  let match;

  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
    match = await createLiveMatch(page, { bestOf: 3 });
  });

  test.afterEach(async ({ page }) => {
    // Stop the page's own flush first: two writers at once lock SQLite.
    await page.evaluate(() => {
      const root = document.getElementById('scoreboard');
      if (root && root.scoreboard) root.scoreboard.session.stop();
    }).catch(() => {});
    await goOnline(page);
    await finishLiveMatch(page, match.pk);
  });

  test('1: a whole match scored offline syncs once and the result matches the taps', async ({ page }) => {
    test.setTimeout(120000);
    await openScoreboard(page, match);
    await goOffline(page);

    // Game 1: to deuce, through it, 13-11 to side 1.
    for (let i = 0; i < 10; i += 1) {
      await tap(page, 'team1');
      await tap(page, 'team2');
    }
    await expectScore(page, 10, 10);
    // At deuce the serve alternates every point: exactly one side serves.
    await expect(page.locator('[data-serve-word]', { hasText: /^serving$/ })).toHaveCount(1);
    await tap(page, 'team1');
    await tap(page, 'team2');
    await tap(page, 'team1', 2);
    await expectScore(page, 0, 0);
    await expect(games(page, 'team1')).toHaveText('1');

    // Undo across the game boundary, then the same point again.
    await page.getByRole('button', { name: 'Undo' }).click();
    await expectScore(page, 12, 11);
    await expect(games(page, 'team1')).toHaveText('0');
    await tap(page, 'team1');
    await expect(games(page, 'team1')).toHaveText('1');

    // Game 2: 3-11 to side 2.
    await tap(page, 'team2', 8);
    await tap(page, 'team1', 3);
    await tap(page, 'team2', 3);
    await expect(games(page, 'team2')).toHaveText('1');
    await expect(page.locator('[data-game-number]')).toHaveText('3');

    // Deciding game: the side-switch prompt appears at 5 with no server.
    await tap(page, 'team1', 5);
    const prompt = page.getByRole('heading', { name: 'Switch ends' });
    await expect(prompt).toBeVisible();
    await page.getByRole('button', { name: 'Done' }).click();
    await expect(prompt).toBeHidden();
    await tap(page, 'team2', 4);
    await tap(page, 'team1', 6);

    // Finished on the phone; the server has heard nothing.
    const finish = page.locator('[data-sb-finish]');
    await expect(finish.getByRole('heading', { name: 'Match finished' })).toBeVisible();
    await expect(finish).toContainText('Game 1: 13–11');
    await expect(finish).toContainText('Game 2: 3–11');
    await expect(finish).toContainText('Game 3: 11–4');
    await expect(page.locator('[data-finish-tally="team1"]')).toHaveText('2');
    await expect(page.locator('[data-finish-tally="team2"]')).toHaveText('1');
    await expect(page.locator('[data-finish-status]')).toContainText(/Offline/);
    expect((await liveState(page, match.pk)).applied_seq).toBe(1);
    expect(page.url()).toContain('/live/');

    // Reconnect: one request carries every op, then the confirm page. The
    // hand-off is a redirect chain, which applyProdCSP cannot carry (see
    // releaseProdCSP); the whole match above ran under the policy.
    const syncs = countSyncs(page);
    await releaseProdCSP(page);
    await goOnline(page);
    await page.waitForURL(new RegExp(`/pingpong/matches/${match.pk}/$`), { timeout: 45000 });
    expect(syncs.answered).toBe(1);

    // The record matches the taps.
    const main = page.locator('main');
    await expect(main).toContainText(/13\s*–\s*11/);
    await expect(main).toContainText(/3\s*–\s*11/);
    await expect(main).toContainText(/11\s*–\s*4/);
    const state = await liveState(page, match.pk);
    expect(state.is_live).toBe(false);
  });

  test('2: a lost ack is re-sent and deduped -- nothing double-counts', async ({ page }) => {
    await openScoreboard(page, match);
    const lost = await dropAcks(page, { times: 1 });
    const syncs = countSyncs(page);

    await tap(page, 'team1', 3);
    await expectScore(page, 3, 0);
    await expectSynced(page);

    expect(lost.dropped).toBe(1);
    expect(syncs.n).toBeGreaterThanOrEqual(2);
    const state = await liveState(page, match.pk);
    expect([state.state.team1_points, state.state.team2_points]).toEqual([3, 0]);
    await expectScore(page, 3, 0);
  });

  test('3: slow, lossy 4G -- the display never goes backwards and the server ends right', async ({ page }) => {
    test.setTimeout(150000);
    await openScoreboard(page, match);
    // Seed 449 loses the 1st request before the server and the 2nd after it
    // applied (asserted below), so both kinds of loss are exercised however
    // the taps happen to batch.
    const chaos = await withLatency(page, { minMs: 2000, maxMs: 4000, dropRate: 0.1, seed: 449 });

    // Record what the scoreboard shows after every render (a MutationObserver
    // callback runs once per render, after all of its writes).
    await page.evaluate(() => {
      const read = (sel) => Number(document.querySelector(sel).textContent);
      window.__shown = [];
      new MutationObserver(() => {
        window.__shown.push([
          read('[data-game-number]'), read('[data-score="team1"]'), read('[data-score="team2"]'),
        ]);
      }).observe(document.getElementById('scoreboard'), { subtree: true, childList: true, characterData: true });
    });

    // 30 quick taps: 11-0, then 10-9 in game 2.
    await tap(page, 'team1', 11);
    for (let i = 0; i < 9; i += 1) {
      await tap(page, 'team1');
      await tap(page, 'team2');
    }
    await tap(page, 'team1');
    await expectScore(page, 10, 9);
    await expectSynced(page, 120000);
    await chaos.stop();

    const shown = await page.evaluate(() => window.__shown);
    for (let i = 1; i < shown.length; i += 1) {
      const [g0, a0, b0] = shown[i - 1];
      const [g1, a1, b1] = shown[i];
      const forward = g1 > g0 || (g1 === g0 && a1 >= a0 && b1 >= b0);
      expect(forward, `display went backwards: ${shown[i - 1]} -> ${shown[i]}`).toBe(true);
    }
    const state = await liveState(page, match.pk);
    expect([state.state.team1_games, state.state.team1_points, state.state.team2_points]).toEqual([1, 10, 9]);
    await expectScore(page, 10, 9);
    expect(chaos.requests).toBeGreaterThan(1);
    expect(chaos.droppedBefore).toBeGreaterThanOrEqual(1);
    expect(chaos.droppedAfter).toBeGreaterThanOrEqual(1);
  });

  test('5: an expired session keeps every point and syncs them after signing in again', async ({ page }) => {
    await openScoreboard(page, match);
    await tap(page, 'team1', 2);
    await expectSynced(page);

    // A CSRF failure first: the sign-in prompt, and the point stays.
    await page.route(SYNC, (route) => route.fulfill({ status: 403, contentType: 'text/html', body: '<h1>Forbidden</h1>' }));
    await tap(page, 'team1');
    const reauth = page.locator('[data-sb-reauth]');
    await expect(reauth).toContainText('Sign in again to save the score');
    await tap(page, 'team2');
    await expectScore(page, 3, 1);
    expect((await sessionView(page)).pending).toBe(2);
    await page.unroute(SYNC);

    // Then the session really is gone: sign in through the prompt's link.
    await page.context().clearCookies();
    // Signing in ends in a redirect to `next`, which applyProdCSP cannot carry.
    await releaseProdCSP(page);
    await reauth.getByRole('link', { name: 'Sign in' }).click();
    await page.waitForURL(/\/accounts\/login\//);
    await page.fill('input[name="username"]', E2E_USERNAME);
    await page.fill('input[name="password"]', E2E_PASSWORD);
    await Promise.all([page.waitForURL(new RegExp(match.url)), page.click('button[type="submit"]')]);

    await expectScore(page, 3, 1);
    await expectSynced(page);
    await expect(reauth).toBeHidden();
    const state = await liveState(page, match.pk);
    expect([state.state.team1_points, state.state.team2_points]).toEqual([3, 1]);
  });

  test('6: a rejected op shows the toast and the score resyncs to the server', async ({ page }) => {
    await openScoreboard(page, match);
    await tap(page, 'team1', 2);
    await expectSynced(page);

    // The next sync's first op is swapped for one the server genuinely
    // refuses (the match already started), so `rejected` is real.
    let swapped = false;
    await page.route(SYNC, async (route) => {
      if (swapped) return route.fallback();
      swapped = true;
      const body = JSON.parse(route.request().postData());
      body.ops[0] = { seq: body.ops[0].seq, type: 'start', initial_server: 'team2' };
      return route.fallback({ postData: JSON.stringify(body) });
    });
    await tap(page, 'team1');
    await expect(page.getByRole('alert')).toContainText('The server didn’t accept a point');
    await expectScore(page, 2, 0);
    await page.unroute(SYNC);

    // Scoring carries on from the server's state.
    await tap(page, 'team2');
    await expectSynced(page);
    const state = await liveState(page, match.pk);
    expect([state.state.team1_points, state.state.team2_points]).toEqual([2, 1]);
    await expectScore(page, 2, 1);
  });

  test('the indicator counts pending points and clears within one flush', async ({ page }) => {
    await openScoreboard(page, match);
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route(SYNC, async (route) => {
      await held;
      await route.fallback();
    });
    await tap(page, 'team1', 3);
    await expect(indicator(page)).toHaveText(/3 points not saved yet/);
    await expect(indicator(page).locator('[data-sync-glyph="pending"] svg')).toBeVisible();
    release();
    await expect(indicator(page)).toBeHidden();
    await page.unroute(SYNC);
  });

  test('a reload with unsynced ops rebuilds them from the outbox and syncs them once', async ({ page }) => {
    await openScoreboard(page, match);
    await page.route(SYNC, (route) => route.abort('failed'));
    await tap(page, 'team1', 12);
    await expect(indicator(page)).toContainText(/Offline/);

    const dialogs = [];
    page.on('dialog', (d) => { dialogs.push(d.type()); d.accept(); });
    await page.reload();
    await expect(score(page, 'team1')).toHaveText('1');
    await expect(games(page, 'team1')).toHaveText('1');
    expect((await sessionView(page)).pending).toBe(12);

    // Open the sheet while still failing; the backoff would otherwise retry
    // on its own and the indicator would vanish under the click.
    await page.locator('[data-sync-indicator]').click();
    const sheet = page.getByRole('dialog', { name: 'Saving the score' });
    await expect(sheet).toContainText('12 changes are saved on this phone');
    await page.unroute(SYNC);
    const syncs = countSyncs(page);
    await sheet.getByRole('button', { name: 'Retry now' }).click();
    await expectSynced(page);
    expect(syncs.answered).toBe(1);
    const state = await liveState(page, match.pk);
    expect([state.state.team1_games, state.state.team1_points]).toEqual([1, 1]);
  });

  test('leaving with unsaved points asks first, in-page, and Stay keeps you', async ({ page }) => {
    await openScoreboard(page, match);
    await goOffline(page);
    await tap(page, 'team2', 2);

    await page.getByRole('link', { name: 'Exit' }).click();
    const sheet = page.getByRole('dialog', { name: 'Not saved yet' });
    await expect(sheet).toBeVisible();
    await expect(sheet).toContainText('2 changes have not reached the server');
    expect(page.url()).toContain('/live/');
    await sheet.getByRole('button', { name: 'Stay' }).click();
    await expect(sheet).toBeHidden();

    // Back online, the `online` event (or the backoff) flushes; then Exit
    // just leaves.
    await goOnline(page);
    await expectSynced(page, 45000);
    await page.getByRole('link', { name: 'Exit' }).click();
    await page.waitForURL(new RegExp(`/pingpong/matches/${match.pk}/$`));
  });

  test('another device scoring the match switches this tab to read-only follow mode', async ({ page }) => {
    await openScoreboard(page, match);
    await tap(page, 'team1');
    await expectSynced(page);
    const applied = (await sessionView(page)).appliedSeq;

    // The other phone sends three points with the next seqs.
    await syncFromAnotherDevice(page, match.pk, [1, 2, 3].map((i) => ({
      seq: applied + i, type: 'point', side: 'team2',
    })));
    await tap(page, 'team1');
    const banner = page.locator('[data-sb-follow]');
    await expect(banner).toContainText('This match is being scored on another device');
    await expectScore(page, 1, 3);
    await tap(page, 'team1');
    await expectScore(page, 1, 3); // read-only

    await banner.getByRole('button', { name: 'Score on this phone instead' }).click();
    await expect(banner).toBeHidden();
    await tap(page, 'team1');
    await expectSynced(page);
    const state = await liveState(page, match.pk);
    expect([state.state.team1_points, state.state.team2_points]).toEqual([2, 3]);
  });

  test('overlays stay closed when the stylesheet never arrives', async ({ page }) => {
    await page.route('**/css/app*.css', (route) => route.abort());
    await page.goto(match.url);
    for (const sel of ['[data-sync-sheet]', '[data-leave-sheet]', '[data-sb-finish]', '[data-sb-follow]',
      '[data-sb-reauth]', '[data-sync-indicator]', '[data-sb-switch]', '[data-sb-toast]']) {
      await expect(page.locator(sel)).toBeHidden();
    }
  });
}

module.exports = { defineOfflineCases, openScoreboard, tap, score, indicator, expectSynced };

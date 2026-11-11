const path = require('path');
const { test, expect } = require('@playwright/test');
const { applyProdCSP, finishLiveMatch, releaseProdCSP } = require('./helpers');
const { startOfflineProxy } = require('./offline-proxy');

/**
 * Two phones, one match (KAN-155): the host (E2E Player, the suite's saved
 * session) and the guest (E2E Partner, `partner.json` from auth.setup.js),
 * each in its own browser context, both under the production CSP.
 *
 * Shared by shared-scoring.spec.js (iphone-safari) and
 * shared-scoring.desktop.spec.js (desktop-chrome). Every test opens its own
 * lobby and finishes its own match, so nothing is left live for another spec.
 *
 * What these prove that pytest and the JS fakes cannot: two real clients,
 * their long-polls and outboxes, against the real server.
 *
 *   - The guest never confirms the result: its navigation to match_confirm is
 *     answered by a stub, so these matches stay unconfirmed and out of every
 *     table the season and rating specs read (a GET of match_confirm
 *     confirms).
 *   - Offline is the offline proxy, never context.setOffline() (KAN-76:
 *     WebKit fails requests before they reach the network layer).
 *   - The e2e server's hold and presence thresholds are short
 *     (settings/e2e.py), so "not seen for" is reached in seconds.
 */

const PARTNER_AUTH_FILE = path.join(__dirname, '.auth', 'partner.json');
const GLICKO_AUTH_FILE = path.join(__dirname, '.auth', 'glicko.json');
// seed_e2e's expired lobby (E2E_EXPIRED_LOBBY_TOKEN).
const EXPIRED_JOIN = '/pingpong/live/join/e2e0expired0lobby000000000000000/';
const CONFIRM_URL = /\/pingpong\/match\/\d+\/confirm\/$/;
const SCOREBOARD_URL = /\/pingpong\/matches\/(\d+)\/live\/$/;

const zone = (page, side) => page.locator(`.tap-zone[data-side="${side}"]`);
const theirs = (page) => page.locator('[data-zone="theirs"]');
const score = (page, side) => page.locator(`[data-score="${side}"]`);
const games = (page, side) => page.locator(`[data-games="${side}"]`);
const presence = (page, side) => page.locator(`[data-presence="${side}"]`);

/** A real tap where the project has touch (WebKit folds rapid synthetic
 *  clicks into double-clicks and drops some); a click on desktop. */
async function press(locator) {
  if (test.info().project.use.hasTouch) await locator.tap();
  else await locator.click();
}

const CONTEXT_KEYS = [
  'viewport', 'screen', 'userAgent', 'deviceScaleFactor', 'isMobile', 'hasTouch',
  'colorScheme', 'reducedMotion', 'locale', 'javaScriptEnabled',
];

/** Another phone: a new context shaped like this project's, with its own
 *  session (or none: anonymous), workers blocked like every project but
 *  the PWA one. */
async function phone(browser, baseURL, { storageState, csp = true } = {}) {
  const use = test.info().project.use;
  const options = { baseURL, serviceWorkers: 'block' };
  for (const key of CONTEXT_KEYS) if (use[key] !== undefined) options[key] = use[key];
  // The runner hands every new context the project's storageState (the
  // host's session) unless told otherwise: an empty one is anonymous.
  options.storageState = storageState || { cookies: [], origins: [] };
  const context = await browser.newContext(options);
  const page = await context.newPage();
  if (csp) await applyProdCSP(page);
  return { context, page };
}

/** Open a lobby for the host (POST like the match form's button, with the
 *  match length the test wants) and land on its page. Returns the join
 *  link's path, read from the page's link text. */
async function openLobby(page, { bestOf }) {
  const html = await (await page.request.get('/pingpong/matches/add/')).text();
  const token = html.match(/name="csrfmiddlewaretoken" value="([^"]+)"/)[1];
  const resp = await page.request.post('/pingpong/live/lobby/new/', {
    form: { csrfmiddlewaretoken: token, best_of: String(bestOf), open_to_club: 'on' },
    maxRedirects: 0,
  });
  const lobbyUrl = resp.headers().location;
  expect(lobbyUrl).toMatch(/\/pingpong\/live\/lobby\/\d+\/$/);
  await page.goto(lobbyUrl);
  const link = page.getByTestId('lobby-url');
  await expect(link).toHaveText(/\/pingpong\/live\/join\/[0-9a-f]{32}\/$/);
  return new URL((await link.innerText()).trim()).pathname;
}

/** The guest opens the link, confirms "Join as E2E Partner?" and lands on
 *  the scoreboard. The join POST redirects, so the CSP route is released
 *  for it (helpers.js) and put back with a reload. Returns the match pk. */
async function join(page, joinUrl) {
  await page.goto(joinUrl);
  await expect(page.getByText('Join as E2E Partner?')).toBeVisible();
  await releaseProdCSP(page);
  await Promise.all([page.waitForURL(SCOREBOARD_URL), press(page.getByTestId('join-match'))]);
  const pk = Number(page.url().match(SCOREBOARD_URL)[1]);
  await applyProdCSP(page);
  await page.reload();
  await expect(page.locator('#scoreboard')).toBeVisible();
  return pk;
}

/** The host's lobby page moves itself to the scoreboard (its poll). */
async function hostFollows(page, pk) {
  await page.waitForURL(new RegExp(`/pingpong/matches/${pk}/live/$`), { timeout: 15000 });
  await expect(page.locator('#scoreboard')).toBeVisible();
}

/** The host picks E2E Player to serve; the guest's board follows. */
async function start(host, guest) {
  await press(host.getByRole('button', { name: 'E2E Player', exact: true }));
  await expect(score(host, 'team1')).toBeVisible();
  await expect(score(guest, 'team1')).toBeVisible({ timeout: 10000 });
}

async function expectScore(page, t1, t2, timeout = 10000) {
  await expect(score(page, 'team1')).toHaveText(String(t1), { timeout });
  await expect(score(page, 'team2')).toHaveText(String(t2), { timeout });
}

async function view(page) {
  return page.evaluate(() => {
    const v = document.getElementById('scoreboard').scoreboard.session.view();
    return { pending: v.pending, phase: v.phase };
  });
}

async function expectSynced(page, timeout = 15000) {
  await expect.poll(async () => (await view(page)).pending, { timeout }).toBe(0);
}

/** Tap `side` n times, confirming the side-switch prompt if it shows. */
async function scorePoints(page, side, n) {
  const prompt = page.locator('[data-sb-switch]');
  for (let i = 0; i < n; i += 1) {
    if (await prompt.isVisible()) await press(page.locator('[data-action="side-switch"]'));
    await press(zone(page, side));
  }
}

/** The guest's landing on match_confirm, without confirming (see top). */
async function stubConfirm(page) {
  await page.route('**/pingpong/match/*/confirm/', (route) => route.fulfill({
    status: 200,
    contentType: 'text/html',
    body: '<!doctype html><title>confirm</title><p>match_confirm reached</p>',
  }));
}

/** Hold this page's live_sync requests until released (a point "in flight"). */
async function holdSync(page) {
  let open;
  const gate = new Promise((resolve) => { open = resolve; });
  const held = { count: 0 };
  const handler = async (route) => {
    held.count += 1;
    await gate;
    try {
      await route.fallback();
    } catch (err) {
      if (!/disposed|Test ended|closed|already handled/i.test(String(err))) throw err;
    }
  };
  await page.route('**/live/sync/', handler);
  return {
    held,
    async release() {
      open();
      await page.unroute('**/live/sync/', handler);
    },
  };
}

/** Wake a page's loops now: what `visibilitychange` to visible does. The
 *  proxy coming back fires no `online` event, so the backoff would wait. */
async function nudge(page) {
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
}

function defineTwoPhoneCases() {
  test.beforeEach(async ({ page }) => {
    test.setTimeout(120000);
    await applyProdCSP(page);
  });

  test('happy path: lobby, join by link, interleaved taps, completion on both', async ({ page, browser, baseURL }) => {
    const joinPath = await openLobby(page, { bestOf: 1 });
    await expect(page.getByTestId('lobby-qr').locator('svg.qr')).toBeVisible();
    const guest = await phone(browser, baseURL, { storageState: PARTNER_AUTH_FILE });
    let pk = null;
    try {
      pk = await join(guest.page, joinPath);
      await hostFollows(page, pk);
      await start(page, guest.page);

      // "My side" on both phones; the opponent's zone is not a button.
      await expect(page.locator('#scoreboard')).toHaveAttribute('data-my-side', 'team1');
      await expect(guest.page.locator('#scoreboard')).toHaveAttribute('data-my-side', 'team2');
      for (const p of [page, guest.page]) {
        expect(await theirs(p).evaluate((el) => el.tagName)).toBe('DIV');
        await expect(p.locator('.tap-zone')).toHaveCount(1);
      }
      await press(theirs(page));
      await page.waitForTimeout(300);
      await expectScore(page, 0, 0);

      // Interleaved: each taps their own zone; both screens agree each time.
      let h = 0;
      let g = 0;
      for (const who of ['host', 'guest', 'guest', 'host', 'host', 'guest', 'host']) {
        if (who === 'host') { await press(zone(page, 'team1')); h += 1; }
        else { await press(zone(guest.page, 'team2')); g += 1; }
        await expectScore(page, h, g);
        await expectScore(guest.page, h, g);
      }

      // Presence: a word and a glyph, each phone about the other.
      await expect(presence(page, 'team2')).toContainText('E2E Partner’s phone · in sync');
      await expect(presence(guest.page, 'team1')).toContainText('E2E Player’s phone · in sync');
      await expect(presence(page, 'team2').locator('[data-presence-glyph="sync"]')).toBeVisible();

      // Today shows the match to the guest too ("You're scoring this match").
      const today = await guest.context.newPage();
      await today.goto('/pingpong/');
      await expect(today.locator(`a[href="/pingpong/matches/${pk}/live/"]`)).toContainText(
        'You’re scoring this match',
      );
      await today.close();

      // Play it out: the host's points end it; both phones go to match_confirm.
      await stubConfirm(guest.page);
      await scorePoints(page, 'team1', 11 - h);
      await Promise.all([
        page.waitForURL(CONFIRM_URL, { timeout: 15000 }),
        guest.page.waitForURL(CONFIRM_URL, { timeout: 15000 }),
      ]);
    } finally {
      await guest.context.close();
      if (pk) await finishLiveMatch(page, pk);
    }
  });

  test('an anonymous guest logs in and comes back to join', async ({ page, browser, baseURL }) => {
    const joinPath = await openLobby(page, { bestOf: 1 });
    const guest = await phone(browser, baseURL);
    let pk = null;
    try {
      await guest.page.goto(joinPath);
      const card = guest.page.getByTestId('live-join');
      await expect(card).toHaveAttribute('data-state', 'anonymous');
      // Logging in redirects twice (resume, then the link): no CSP route.
      await releaseProdCSP(guest.page);
      await press(card.getByRole('link', { name: 'Log in' }));
      await guest.page.waitForURL(/\/accounts\/login\//);
      await guest.page.fill('input[name="username"]', 'e2e-partner');
      await guest.page.fill('input[name="password"]', 'e2e-local-only');
      await Promise.all([
        guest.page.waitForURL(new RegExp(`${joinPath}$`)),
        guest.page.click('button[type="submit"]'),
      ]);
      await applyProdCSP(guest.page);
      pk = await join(guest.page, joinPath);
      await hostFollows(page, pk);
      await start(page, guest.page);
      await press(zone(guest.page, 'team2'));
      await expectScore(page, 0, 1);
    } finally {
      await guest.context.close();
      if (pk) await finishLiveMatch(page, pk);
    }
  });

  test('one phone offline for a game: both converge, no point twice', async ({ page, browser, baseURL }) => {
    const joinPath = await openLobby(page, { bestOf: 3 });
    const proxy = await startOfflineProxy(baseURL);
    const guest = await phone(browser, proxy.origin, { storageState: PARTNER_AUTH_FILE });
    let pk = null;
    try {
      pk = await join(guest.page, proxy.origin + joinPath);
      await hostFollows(page, pk);
      await start(page, guest.page);
      await expectSynced(guest.page);

      // Game 1: the guest's phone loses the network. The host scores 4
      // online; the guest scores 11 on the phone alone.
      proxy.setMode('offline');
      await scorePoints(page, 'team1', 4);
      await scorePoints(guest.page, 'team2', 11);
      await expect(games(guest.page, 'team2')).toHaveText('1');
      await expect.poll(async () => (await view(guest.page)).pending).toBe(11);
      await expectSynced(page);
      await expectScore(page, 4, 0);
      // The host sees the guest go quiet: "last seen ... ago" (never amber).
      await expect(presence(page, 'team2')).toContainText('E2E Partner’s phone · last seen', { timeout: 40000 });
      await expect(presence(page, 'team2').locator('[data-presence-glyph="behind"]')).toBeVisible();

      // Back online: the guest's 11 land after the host's 4 (arrival order).
      proxy.setMode('online');
      await nudge(guest.page);
      await expectSynced(guest.page, 30000);
      for (const p of [page, guest.page]) {
        await expect(games(p, 'team2')).toHaveText('1', { timeout: 15000 });
        await expectScore(p, 0, 0);
      }
      await expect(presence(page, 'team2')).toContainText('in sync', { timeout: 30000 });

      // Game 2 online, the guest takes the match 2-0.
      await stubConfirm(guest.page);
      await scorePoints(guest.page, 'team2', 11);
      await Promise.all([
        page.waitForURL(CONFIRM_URL, { timeout: 20000 }),
        guest.page.waitForURL(CONFIRM_URL, { timeout: 20000 }),
      ]);
      // match_confirm hands the host on to match detail: every point once.
      await page.goto(`/pingpong/matches/${pk}/`);
      await expect(page.getByText('Scored live, point by point · 26 points')).toBeVisible();
    } finally {
      await guest.context.close();
      await proxy.close();
      if (pk) await finishLiveMatch(page, pk);
    }
  });

  test('conditional undo: the guest\'s point in flight survives the host\'s undo', async ({ page, browser, baseURL }) => {
    const joinPath = await openLobby(page, { bestOf: 1 });
    const guest = await phone(browser, baseURL, { storageState: PARTNER_AUTH_FILE });
    let pk = null;
    try {
      pk = await join(guest.page, joinPath);
      await hostFollows(page, pk);
      await start(page, guest.page);
      await press(zone(page, 'team1'));
      await expectScore(guest.page, 1, 0);
      await expectSynced(page);
      await expect(page.locator('[data-undo-label]')).toHaveText('Undo E2E Player’s point (1–0)');

      // The guest's point leaves the phone but has not landed; the host,
      // who has not seen it, undoes "my point, #1". Both are held.
      const guestSync = await holdSync(guest.page);
      const hostSync = await holdSync(page);
      await press(zone(guest.page, 'team2'));
      await expect.poll(() => guestSync.held.count).toBeGreaterThan(0);
      await press(page.locator('[data-action="undo"]'));
      await expectScore(page, 0, 0);
      await expect.poll(() => hostSync.held.count).toBeGreaterThan(0);

      // The guest's point lands first...
      await guestSync.release();
      await expectSynced(guest.page);
      // ...so the host's undo names a point that is no longer trailing:
      // "stale undo", nothing deleted, said in a toast.
      await hostSync.release();
      await expect(page.locator('[data-sb-toast]')).toContainText('Undo didn’t apply');
      await expectSynced(page);
      await expectScore(page, 1, 1);
      await expectScore(guest.page, 1, 1);
    } finally {
      await guest.context.close();
      if (pk) await finishLiveMatch(page, pk);
    }
  });

  test('takeover: the guest\'s phone dies, the host scores for both and finishes', async ({ page, browser, baseURL }) => {
    const joinPath = await openLobby(page, { bestOf: 1 });
    const guest = await phone(browser, baseURL, { storageState: PARTNER_AUTH_FILE });
    let pk = null;
    try {
      pk = await join(guest.page, joinPath);
      await hostFollows(page, pk);
      await start(page, guest.page);
      await press(zone(guest.page, 'team2'));
      await expectScore(page, 0, 1);
      const bar = page.locator('[data-sb-shared-actions]');
      await expect(bar).toBeHidden();

      await guest.context.close(); // the battery dies

      const scoreBoth = bar.getByRole('button', { name: 'Score for both' });
      await expect(scoreBoth).toBeVisible({ timeout: 60000 });
      await expect(presence(page, 'team2')).toContainText('E2E Partner’s phone · not seen for');
      await expect(presence(page, 'team2').locator('[data-presence-glyph="lost"]')).toBeVisible();
      await press(scoreBoth);

      // Two zones again: both are buttons, and the guest's side scores.
      const root = page.locator('#scoreboard');
      await expect(root).not.toHaveAttribute('data-my-side', /team/);
      await expect(page.locator('.tap-zone')).toHaveCount(2);
      await expect(bar).toBeHidden();
      // Reversible, from the Phones sheet.
      await press(page.locator('[data-action="phones"]'));
      const phones = page.locator('[data-phones-sheet]');
      await expect(phones.getByRole('button', { name: 'Stop scoring for both' })).toBeVisible();
      await press(phones.getByRole('button', { name: 'Close' }));
      await expect(phones).toBeHidden();
      await scorePoints(page, 'team2', 2);
      await expectScore(page, 0, 3);
      await expectSynced(page);
      await scorePoints(page, 'team1', 11);
      await page.waitForURL(CONFIRM_URL, { timeout: 15000 });
    } finally {
      if (pk) await finishLiveMatch(page, pk);
    }
  });

  test('the host removes the guest\'s phone; the guest is told and stops', async ({ page, browser, baseURL }) => {
    const joinPath = await openLobby(page, { bestOf: 1 });
    const guest = await phone(browser, baseURL, { storageState: PARTNER_AUTH_FILE });
    let pk = null;
    try {
      pk = await join(guest.page, joinPath);
      await hostFollows(page, pk);
      await start(page, guest.page);
      await press(zone(guest.page, 'team2'));
      await expectScore(page, 0, 1);

      // Behind an in-page sheet, never confirm(): Cancel leaves it be.
      const dialogs = [];
      page.on('dialog', (d) => { dialogs.push(d.type()); d.dismiss().catch(() => {}); });
      const phonesButton = page.getByRole('button', { name: 'Phones' });
      const ask = page.getByRole('button', { name: 'Remove E2E Partner’s phone' });
      await press(phonesButton);
      await expect(page.locator('[data-phones-presence]')).toHaveText('E2E Partner’s phone · in sync');
      await press(ask);
      const sheet = page.locator('[data-remove-sheet]');
      await expect(sheet).toBeVisible();
      await press(sheet.getByRole('button', { name: 'Cancel' }));
      await expect(sheet).toBeHidden();
      await expect(page.locator('.tap-zone')).toHaveCount(1); // nothing happened
      await press(phonesButton);
      await press(ask);
      await press(sheet.getByRole('button', { name: 'Remove', exact: true }));
      await expect(sheet).toBeHidden();

      // The guest learns it from its poll (403 "removed"), not as "sign in".
      const removed = guest.page.locator('[data-sb-removed]');
      await expect(removed).toBeVisible({ timeout: 15000 });
      await expect(removed).toContainText('You were removed from scoring this match');
      await expect(guest.page.locator('[data-sb-reauth]')).toBeHidden();
      await press(zone(guest.page, 'team2'));
      expect((await view(guest.page)).pending).toBe(0);

      expect(dialogs).toEqual([]);

      // The host scores both now.
      await expect(page.locator('.tap-zone')).toHaveCount(2);
      await scorePoints(page, 'team2', 1);
      await expectScore(page, 0, 2);
      await expectSynced(page);
    } finally {
      await guest.context.close();
      if (pk) await finishLiveMatch(page, pk);
    }
  });

  test('dead links say why; the guest\'s own link takes it back to the match', async ({ page, browser, baseURL }) => {
    const guest = await phone(browser, baseURL, { storageState: PARTNER_AUTH_FILE });
    const other = await phone(browser, baseURL, { storageState: GLICKO_AUTH_FILE });
    let pk = null;
    try {
      // Expired (seeded).
      const expired = await guest.page.goto(EXPIRED_JOIN);
      expect(expired.status()).toBe(410);
      await expect(guest.page.getByText('This link has expired. Ask for a new code.')).toBeVisible();

      // Already joined: anyone else is told so...
      const joinPath = await openLobby(page, { bestOf: 1 });
      pk = await join(guest.page, joinPath);
      await hostFollows(page, pk);
      const joined = await other.page.goto(joinPath);
      expect(joined.status()).toBe(410);
      await expect(other.page.getByText('Someone else has already joined this match.')).toBeVisible();

      // ...and the guest reopening its link is back on the scoreboard.
      await releaseProdCSP(guest.page);
      await guest.page.goto(joinPath);
      await expect(guest.page).toHaveURL(new RegExp(`/pingpong/matches/${pk}/live/$`));
      await expect(guest.page.locator('#scoreboard')).toBeVisible();
    } finally {
      await guest.context.close();
      await other.context.close();
      if (pk) await finishLiveMatch(page, pk);
    }
  });
}

module.exports = { defineTwoPhoneCases };

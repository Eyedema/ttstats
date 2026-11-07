const { expect } = require('@playwright/test');

/** A Content-Security-Policy with no 'unsafe-eval'.
 *
 *  Dev sends no CSP at all, which is why the mobile drawer could be broken in
 *  production while pytest, a manual browser pass and this entire suite were
 *  green. Specs that exercise interactive behaviour should apply it.
 *
 *  This is deliberately STRICTER than what prod.py now sends: production had
 *  to add 'unsafe-eval' so the Alpine-based scoreboard works at all. Keeping
 *  it out here means the drawer -- which is plain JS precisely so it does not
 *  need eval -- cannot quietly regress onto a framework expression and take
 *  the whole navigation down again. If you find yourself adding 'unsafe-eval'
 *  to this constant to make a test pass, that is the regression. */
const PROD_CSP = [
  "script-src 'self' 'unsafe-inline'",
  "frame-ancestors 'none'",
  "connect-src 'self'",
  "default-src 'self'",
  "img-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  "form-action 'self'",
  "font-src 'self'",
  // The service worker and the web app manifest. Both would fall through to
  // default-src and happen to work, but naming them here means a future
  // default-src change cannot silently kill push notifications -- and on iOS,
  // a blocked manifest means no home-screen install, which means no push at
  // all.
  "worker-src 'self'",
  "manifest-src 'self'",
].join('; ');

/** Serve every response with the production CSP. */
async function applyProdCSP(page) {
  await page.route('**/*', async (route) => {
    try {
      const response = await route.fetch();
      await route.fulfill({
        response,
        headers: { ...response.headers(), 'content-security-policy': PROD_CSP },
      });
    } catch (err) {
      // A deferred script or a prefetch (KAN-124) still in flight when the
      // page navigates or the test ends: its response is gone, and nothing
      // is waiting for it. Anything else is a real failure.
      if (!/disposed|Test ended|closed/i.test(String(err))) throw err;
    }
  });
}

const E2E_USERNAME = 'e2e';
const E2E_PASSWORD = 'e2e-local-only';

/** Log in through the real form. Throwaway local-only account, seeded by
 *  `manage.py seed_e2e`, which refuses to run outside DEBUG. */
async function login(page) {
  await page.goto('/accounts/login/');
  await page.fill('input[name="username"]', E2E_USERNAME);
  await page.fill('input[name="password"]', E2E_PASSWORD);
  await Promise.all([
    page.waitForURL(/\/pingpong\//),
    page.click('button[type="submit"]'),
  ]);
}

/** The mobile drawer panel. Identified by role so the spec does not depend on
 *  the Tailwind class soup, which changes for visual reasons. */
function drawer(page) {
  return page.locator('#mobile-menu-panel');
}

function drawerScrim(page) {
  return page.locator('#mobile-menu-scrim');
}

/** Assert that nothing belonging to the drawer is covering the page.
 *  Checks the scrim too: a transparent-but-present scrim swallows every tap
 *  on the page underneath, which is its own flavour of "can't be closed". */
async function expectDrawerClosed(page) {
  await expect(drawer(page)).toBeHidden();
  await expect(drawerScrim(page)).toBeHidden();
}

/** Wait until the drawer has finished sliding.
 *
 *  `toBeVisible()` resolves the instant display stops being none -- which is
 *  the *start* of the 300ms enter transition, with the panel still translated
 *  fully off-screen. Measuring geometry there reports -256 and looks like a
 *  bug in the app rather than in the test. */
async function waitForDrawerSettled(page) {
  await expect
    .poll(async () => {
      const box = await drawer(page).boundingBox();
      return box ? Math.round(box.x) : null;
    }, { timeout: 2000 })
    .toBe(0);
}

/** Open the seeded completed live-scored match ("E2E Momentum").
 *
 *  The match list links to a match by icon only, so there is no accessible
 *  name to match on -- `getByRole('link', {name: /E2E Momentum/})` finds the
 *  PLAYER link and lands on player detail, where every later assertion then
 *  fails for the wrong reason. Read the row's href instead, which also works
 *  at a viewport where the table is laid out differently.
 */
async function openMomentumMatch(page) {
  await page.goto('/pingpong/matches/');
  const row = page.locator('tr').filter({ hasText: 'E2E Momentum' }).first();
  const href = await row
    .locator('a[href^="/pingpong/matches/"]:not([href*="edit"])')
    .first()
    .getAttribute('href');
  if (!href) throw new Error('No seeded momentum match in the match list');
  await page.goto(href);
  return href;
}

/** Open a player's detail page by name.
 *
 *  Same trap as the match list: a player's name appears in several places, and
 *  some of the matching nodes are hidden (the desktop table at phone width),
 *  so `.tap()` on a locator that resolves to one of them just times out. Read
 *  the href and navigate.
 */
async function openPlayerByName(page, name) {
  await page.goto('/pingpong/players/');
  // The name is not inside the anchor -- it sits in a sibling cell of the
  // same row -- so filtering the anchors themselves by text finds nothing.
  // Scope to the row that mentions the player, then take its detail link.
  const href = await page
    .locator('tr')
    .filter({ hasText: name })
    .first()
    .locator('a[href^="/pingpong/players/"]:not([href*="edit"])')
    .first()
    .getAttribute('href');
  if (!href) throw new Error('No player named ' + name + ' in the player list');
  await page.goto(href);
  return href;
}

// --- Network chaos (KAN-83) --------------------------------------------------
//
// WebKit has no CDP throttling, so latency and loss are simulated with
// page.route. Playwright runs the most recently registered matching route
// first, so these are registered AFTER applyProdCSP() and hand the request on
// with route.fallback() -- which lands in applyProdCSP's handler. A request
// they answer themselves (abort, or route.fetch() for a lost ack) never needs
// the CSP header: it is a JSON sync, not a document.

/** Deterministic PRNG (mulberry32): chaos specs must be reproducible. */
function seededRandom(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const SYNC_URL = '**/live/sync/';

/** Drop every '**\/*' route (applyProdCSP's, and goOffline's if present).
 *
 *  applyProdCSP answers each request with route.fetch() + fulfill(). For a
 *  navigation answered with a redirect (the scoreboard handing off to
 *  match_confirm, the login form's `next`) Playwright does not follow it in
 *  the fetch, and WebKit does not follow a fulfilled 3xx either: the page is
 *  left on the old document. A spec whose page has already proved itself
 *  under the CSP releases the routes before such a navigation. */
async function releaseProdCSP(page) {
  await page.unroute('**/*');
  page.__offlineHandler = null;
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Kill the network for this page: the context's offline flag (so
 *  navigator.onLine and the online/offline events follow) plus an abort-all
 *  route, because a routed request is otherwise fetched by Playwright itself
 *  and would reach the server regardless of the flag. */
async function goOffline(page) {
  const handler = (route) => route.abort('internetdisconnected');
  page.__offlineHandler = handler;
  await page.route('**/*', handler);
  await page.context().setOffline(true);
}

async function goOnline(page) {
  if (page.__offlineHandler) {
    await page.unroute('**/*', page.__offlineHandler);
    page.__offlineHandler = null;
  }
  await page.context().setOffline(false);
}

/** Slow, lossy 4G for the sync endpoint: every request waits minMs..maxMs,
 *  and `dropRate` of them fail -- half before reaching the server (request
 *  lost), half after it applied the ops (answer lost). Returns live counters. */
async function withLatency(page, { minMs = 2000, maxMs = 4000, dropRate = 0.1, seed = 1, url = SYNC_URL } = {}) {
  const random = seededRandom(seed);
  const stats = { requests: 0, droppedBefore: 0, droppedAfter: 0 };
  const handler = async (route) => {
    stats.requests += 1;
    const delay = minMs + random() * (maxMs - minMs);
    const drop = random() < dropRate;
    const after = random() < 0.5;
    await sleep(delay);
    try {
      if (drop && !after) {
        stats.droppedBefore += 1;
        await route.abort('failed');
      } else if (drop) {
        stats.droppedAfter += 1;
        await route.fetch();
        await route.abort('failed');
      } else {
        await route.fallback();
      }
    } catch (err) {
      if (!/disposed|Test ended|closed|already handled/i.test(String(err))) throw err;
    }
  };
  await page.route(url, handler);
  stats.stop = () => page.unroute(url, handler);
  return stats;
}

/** Lost acks: the next `times` syncs reach the server and are applied, and
 *  the page is told the request failed. */
async function dropAcks(page, { times = 1, url = SYNC_URL } = {}) {
  const stats = { dropped: 0 };
  const handler = async (route) => {
    if (stats.dropped >= times) return route.fallback();
    stats.dropped += 1;
    await route.fetch();
    return route.abort('failed');
  };
  await page.route(url, handler);
  stats.stop = () => page.unroute(url, handler);
  return stats;
}

// --- Live matches for the chaos specs ---------------------------------------

async function csrfFrom(page, path = '/pingpong/matches/add/') {
  const html = await (await page.request.get(path)).text();
  const token = html.match(/name="csrfmiddlewaretoken" value="([^"]+)"/);
  if (!token) throw new Error('No CSRF token on ' + path);
  return { html, token: token[1] };
}

function optionValue(html, select, label) {
  const block = html.match(new RegExp(`<select[^>]*name="${select}"[\\s\\S]*?</select>`));
  if (!block) throw new Error('No <select name=' + select + '>');
  for (const m of block[0].matchAll(/<option value="(\d+)"[^>]*>([^<]*)<\/option>/g)) {
    if (m[2].trim() === label) return m[1];
  }
  throw new Error('No option ' + label + ' in ' + select);
}

/** A fresh live match, E2E Player (scorekeeper) vs E2E Chaos, created
 *  through the real "Score live" form. Returns { pk, url }. */
async function createLiveMatch(page, { bestOf = 3, opponent = 'E2E Chaos' } = {}) {
  const { html, token } = await csrfFrom(page);
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const resp = await page.request.post('/pingpong/matches/add/', {
    form: {
      csrfmiddlewaretoken: token,
      player1: optionValue(html, 'player1', 'E2E Player'),
      player2: optionValue(html, 'player2', opponent),
      player3: '',
      player4: '',
      is_double: 'False',
      date_played: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`,
      location: '',
      match_type: 'casual',
      best_of: String(bestOf),
      notes: '',
      start_live: '1',
    },
    maxRedirects: 0,
  });
  const location = resp.headers().location || '';
  const pk = (location.match(/matches\/(\d+)\/live\//) || [])[1];
  if (!pk) throw new Error(`Score live did not redirect to a scoreboard (${resp.status()} ${location})`);
  return { pk: Number(pk), url: `/pingpong/matches/${pk}/live/` };
}

async function liveState(page, pk) {
  return (await page.request.get(`/pingpong/matches/${pk}/live/state/`)).json();
}

/** Finish a live match through live_sync, so a spec that stops mid-match
 *  leaves no live match behind for the next one's "Resume" link. */
async function finishLiveMatch(page, pk) {
  const state = await liveState(page, pk);
  if (!state.is_live) return;
  const { token } = await csrfFrom(page);
  let seq = state.applied_seq;
  const ops = [];
  if (!state.state.started) ops.push({ seq: ++seq, type: 'start', initial_server: 'team1' });
  for (let i = 0; i < 80; i += 1) ops.push({ seq: ++seq, type: 'point', side: 'team1' });
  await page.request.post(`/pingpong/matches/${pk}/live/sync/`, {
    data: { ops },
    headers: { 'X-CSRFToken': token },
  });
}

/** Ops from "another phone": straight to live_sync, seqs chosen by the caller. */
async function syncFromAnotherDevice(page, pk, ops) {
  const { token } = await csrfFrom(page);
  return page.request.post(`/pingpong/matches/${pk}/live/sync/`, {
    data: { ops },
    headers: { 'X-CSRFToken': token },
  });
}

module.exports = {
  releaseProdCSP,
  seededRandom,
  goOffline,
  goOnline,
  withLatency,
  dropAcks,
  createLiveMatch,
  finishLiveMatch,
  liveState,
  syncFromAnotherDevice,
  openMomentumMatch,
  openPlayerByName,
  PROD_CSP,
  applyProdCSP,
  waitForDrawerSettled,
  E2E_USERNAME,
  E2E_PASSWORD,
  login,
  drawer,
  drawerScrim,
  expectDrawerClosed,
};

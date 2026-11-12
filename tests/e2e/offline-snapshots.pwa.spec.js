const base = require('@playwright/test');
const { PROD_CSP } = require('./helpers');
const { startOfflineProxy } = require('./offline-proxy');

/**
 * KAN-131 offline reading: a page seen once reopens with no signal from a
 * saved copy, ALWAYS labelled; never when the network answers; never another
 * identity's (KAN-133 / KAN-145, cases from KAN-138).
 *
 * Runs in `iphone-pwa`, the one project where the worker may control the
 * page. The network is cut by `offline-proxy.js` (offline / hang / 502), never
 * context.setOffline() (WebKit fails the loads before the worker runs, so
 * "not served" would pass vacuously) and never page.route() (a controlling
 * worker hides requests from it). The proxy adds the production CSP to every
 * HTML answer instead of applyProdCSP(), so a stored copy carries it too.
 * Every "not served" check is paired with one that IS served over the same
 * dead network.
 *
 * Users (manage.py seed_e2e): e2e-reader (A) and e2e-reader-b (B) have the
 * offline_snapshots flag on, e2e-reader-off has it off. Nobody else does, so
 * no other spec ever sees a snapshot. These specs log in and out, so they
 * start from an empty cookie jar, never the shared `e2e` session.
 */
const PASSWORD = 'e2e-local-only';
const READER = 'e2e-reader';
const READER_B = 'e2e-reader-b';
const READER_OFF = 'e2e-reader-off';
const NAV_TIMEOUT_MS = 4000; // sw.js

const test = base.test.extend({
  // Not `proxy`: that name is Playwright's own context option. Depends on
  // `context` so it is torn down FIRST: a request the proxy is holding
  // (`hang`) keeps the worker's fetch event alive, and WebKit will not close
  // a context whose worker is still busy.
  net: async ({ baseURL, context }, use) => {
    const net = await startOfflineProxy(baseURL, { csp: PROD_CSP });
    await use(net);
    await net.close();
  },
});
const { expect } = base;

test.use({ storageState: { cookies: [], origins: [] } });
// Each test logs in and visits several pages first.
test.describe.configure({ timeout: 60000 });

test.beforeEach(async ({ page }) => {
  // CSP violations, for the "served copy keeps its policy" checks. Chart.js's
  // one `eval` refusal is expected under PROD_CSP (frontend-verification).
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', (event) => {
      if (event.blockedURI !== 'eval') {
        window.__cspViolations.push(`${event.violatedDirective} ${event.blockedURI}`);
      }
    });
  });
});

async function waitForControl(page) {
  await expect
    .poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller)), {
      timeout: 10000,
    })
    .toBe(true);
}

async function login(page, net, username) {
  await page.goto(net.origin + '/accounts/login/');
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([page.waitForURL(/\/pingpong\//), page.click('button[type="submit"]')]);
  await waitForControl(page);
}

async function logout(page) {
  await Promise.all([
    page.waitForURL(/\/accounts\/login\//),
    page.evaluate(() => document.querySelector('form[data-sw-purge]').requestSubmit()),
  ]);
}

function snapshotCaches(page) {
  return page.evaluate(async () =>
    (await caches.keys()).filter((name) => name.startsWith('ttstats-snap-'))
  );
}

/** Path + query of every stored snapshot, any partition. */
function storedPaths(page) {
  return page.evaluate(async () => {
    const paths = [];
    for (const name of await caches.keys()) {
      if (!name.startsWith('ttstats-snap-')) continue;
      for (const request of await (await caches.open(name)).keys()) {
        const url = new URL(request.url);
        paths.push(url.pathname + url.search);
      }
    }
    return paths;
  });
}

/** Open `path` online until the worker has stored it. The first controlled
 *  navigation can race the page's identity message, hence the retry. */
async function visit(page, net, path) {
  await expect(async () => {
    await page.goto(net.origin + path);
    await expect.poll(() => storedPaths(page), { timeout: 3000 }).toContain(path);
  }).toPass({ timeout: 20000 });
}

/** The reader's seeded match (the only finished one against Reader Off). */
async function readerMatchPath(page, net) {
  await page.goto(net.origin + '/pingpong/matches/');
  const hrefs = await page
    .locator('a[href^="/pingpong/matches/"]')
    .evaluateAll((links) => links.map((a) => a.getAttribute('href')));
  const path = hrefs.find((href) => /^\/pingpong\/matches\/\d+\/$/.test(href));
  if (!path) throw new Error('No seeded match in the reader\'s match list');
  return path;
}

function freshness(page) {
  return page.evaluate(() => document.documentElement.dataset.freshness);
}

const banner = (page) => page.locator('[data-snapshot-banner]');

async function expectLive(page) {
  expect(await freshness(page)).toBe('network');
  await expect(banner(page)).toBeHidden();
}

/** A labelled saved copy, stored no earlier than `since`. */
async function expectSnapshot(page, since) {
  expect(await freshness(page)).toBe('snapshot');
  await expect(banner(page)).toBeVisible();
  const shown = await page.evaluate(() => {
    const at = document.documentElement.dataset.snapshotAt;
    const time = document.querySelector('time[data-snapshot-time]');
    const d = new Date(at);
    const pad = (n) => String(n).padStart(2, '0');
    return {
      at: Date.parse(at),
      datetime: time.getAttribute('datetime'),
      text: time.textContent,
      clock: pad(d.getHours()) + ':' + pad(d.getMinutes()),
    };
  });
  expect(shown.at).toBeGreaterThanOrEqual(since - 2000);
  expect(shown.at).toBeLessThanOrEqual(Date.now() + 2000);
  expect(new Date(shown.datetime).getTime()).toBe(shown.at);
  // "14:32" (or "Mon 23:59" across midnight): the local clock time stored.
  expect(shown.text.endsWith(shown.clock)).toBe(true);
  await expect(banner(page)).toContainText(`No connection — saved copy from ${shown.text}`, {
    useInnerText: true,
  });
  await expect(banner(page)).not.toContainText('may be out of date', { useInnerText: true });
  expect(await page.evaluate(() => window.__cspViolations)).toEqual([]);
}

/** The offline page (KAN-137): what a page with no copy gets, no network. */
async function expectOfflinePage(page) {
  await expect(page.locator('[data-offline-page]')).toBeVisible();
  expect(await freshness(page)).toBeUndefined();
  await expect(banner(page)).toHaveCount(0);
}

async function staticStillServed(page) {
  // The worker is alive and answering over the same dead network: a hashed
  // stylesheet comes out of its static cache.
  const href = await page.evaluate(
    () => [...document.styleSheets].map((s) => s.href).find((h) => h && h.includes('/css/app.')) || ''
  );
  expect(href).not.toBe('');
  const status = await page.evaluate((h) => fetch(h).then((r) => r.status, () => 'error'), href);
  expect(status).toBe(200);
}

test.describe('offline snapshots', () => {
  test('online, a stored copy is never served', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/leaderboard/');

    // Doctor the stored copy so serving it would be visible.
    await page.evaluate(async () => {
      const name = (await caches.keys()).find((n) => n.startsWith('ttstats-snap-'));
      const cache = await caches.open(name);
      const key = location.origin + '/pingpong/leaderboard/';
      const hit = await cache.match(key);
      const html = (await hit.text()).replace('</main>', '<p data-stale-plant>STALE</p></main>');
      await cache.put(key, new Response(html, { headers: hit.headers }));
    });

    await page.reload();
    await expectLive(page);
    await expect(page.locator('[data-stale-plant]')).toHaveCount(0);

    // The copy WAS eligible: offline, the (now refreshed) copy is served.
    const since = Date.now();
    net.setMode('offline');
    await page.reload();
    await expectSnapshot(page, since - 10000);
    await expect(page.locator('[data-stale-plant]')).toHaveCount(0);
  });

  test('offline: Today, the Table and a match reopen labelled', async ({ page, net }) => {
    await login(page, net, READER);
    const match = await readerMatchPath(page, net);
    const since = Date.now();
    const pages = ['/pingpong/', '/pingpong/leaderboard/', match];
    for (const path of pages) await visit(page, net, path);

    net.setMode('offline');
    for (const path of pages) {
      const response = await page.goto(net.origin + path);
      expect(response.ok()).toBe(true);
      await expectSnapshot(page, since);
      // The page itself, not an empty shell: the reader's name is on it.
      await expect(page.locator('main')).toContainText('E2E Reader');
    }
    // The stored copy kept the page's own CSP.
    const csp = await page.evaluate(async (p) => {
      const name = (await caches.keys()).find((n) => n.startsWith('ttstats-snap-'));
      const hit = await (await caches.open(name)).match(location.origin + p);
      return hit.headers.get('Content-Security-Policy');
    }, match);
    expect(csp).toBe(PROD_CSP);
  });

  test('a page the server did not opt in is never served; one it did is', async ({ page, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/');
    await page.goto(net.origin + '/pingpong/matches/add/');
    await expect(page.locator('form')).not.toHaveCount(0);
    expect(await storedPaths(page)).not.toContain('/pingpong/matches/add/');

    net.setMode('offline');
    // No copy of the form: the offline page (KAN-137), never the form.
    await page.goto(net.origin + '/pingpong/matches/add/');
    await expectOfflinePage(page);
    await expect(page.locator('form')).toHaveCount(0);
    // Never visited: no copy, the offline page.
    await page.goto(net.origin + '/pingpong/calendar/');
    await expectOfflinePage(page);
    // Same dead network, and the opted-in page opens.
    await page.goto(net.origin + '/pingpong/');
    await expectSnapshot(page, 0);
  });

  test('lie-fi: a network that never answers gives up within the timeout', async ({ page, net }) => {
    await login(page, net, READER);
    const since = Date.now();
    await visit(page, net, '/pingpong/leaderboard/');

    net.setMode('hang');
    try {
      const started = Date.now();
      // Not "load": requests the worker does not answer (the manifest) hang
      // too, as on real lie-fi, and hold the load event.
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expectSnapshot(page, since);
      // When the document's response started, by the page's own clock.
      const responseStart = await page.evaluate(
        () => performance.getEntriesByType('navigation')[0].responseStart
      );
      expect(responseStart).toBeLessThan(NAV_TIMEOUT_MS + 500);
      expect(Date.now() - started).toBeLessThan(NAV_TIMEOUT_MS + 3000);

      // The network recovers: the held navigation is answered late, a fresh
      // copy is stored and the page is told (offline.js marks the root).
      const before = await page.evaluate(() => document.documentElement.dataset.snapshotAt);
      net.release();
      await expect
        .poll(() => page.evaluate(() => document.documentElement.hasAttribute('data-snapshot-fresh')), {
          timeout: 10000,
        })
        .toBe(true);
      const after = await page.evaluate(async () => {
        const name = (await caches.keys()).find((n) => n.startsWith('ttstats-snap-'));
        const hit = await (await caches.open(name)).match(location.origin + '/pingpong/leaderboard/');
        return hit.headers.get('X-Snapshot-At');
      });
      expect(Date.parse(after)).toBeGreaterThan(Date.parse(before));
    } finally {
      // Release anything still held: WebKit will not tear down a page with
      // loads in flight.
      net.setMode('online');
    }
  });

  test('the server answering 502 serves the labelled copy', async ({ page, net }) => {
    await login(page, net, READER);
    const since = Date.now();
    await visit(page, net, '/pingpong/');

    net.setMode('502');
    await page.reload();
    await expectSnapshot(page, since);
    // Never visited, so no copy: the offline page, worded for the server.
    await page.goto(net.origin + '/pingpong/calendar/');
    await expectOfflinePage(page);
    await expect(page.locator('h1')).toContainText("TTStats isn't answering", { useInnerText: true });
  });

  test('an expired session goes to the login page, never a copy', async ({ page, context, net }) => {
    await login(page, net, READER);
    await visit(page, net, '/pingpong/');

    // The session ends (expiry, or logged out elsewhere): Django answers 302.
    await context.clearCookies();
    await page.goto(net.origin + '/pingpong/');
    await expect(page).toHaveURL(/\/accounts\/login\//);
    await expect(page.locator('input[name="username"]')).toBeVisible();
    await expect(banner(page)).toHaveCount(0);
    // The redirect evicted the copy, and the logged-out page dropped the rest.
    await expect.poll(() => snapshotCaches(page)).toEqual([]);
  });
});

test.describe('whose copies (the data-leak boundary)', () => {
  test("after A logs out and B logs in, none of A's pages exist or open", async ({ page, net }) => {
    await login(page, net, READER);
    const match = await readerMatchPath(page, net);
    for (const path of ['/pingpong/', '/pingpong/leaderboard/', match]) await visit(page, net, path);
    const [aPartition] = await snapshotCaches(page);
    expect(aPartition).toMatch(/^ttstats-snap-ttstats-v5-[0-9a-f]{10}-[0-9a-f]{16}$/);

    await logout(page);
    await login(page, net, READER_B);
    await visit(page, net, '/pingpong/');
    const names = await snapshotCaches(page);
    expect(names).not.toContain(aPartition);
    expect(names).toHaveLength(1);

    net.setMode('offline');
    // A's match and table: no copy for B, so the offline page (KAN-137).
    for (const path of [match, '/pingpong/leaderboard/']) {
      await page.goto(net.origin + path);
      await expectOfflinePage(page);
      // Its list is B's one saved page, none of A's.
      const listed = page.locator('[data-offline-saved] a[data-item-link]');
      await expect(listed).toHaveCount(1);
      expect(await listed.getAttribute('href')).toBe('/pingpong/');
    }
    // B's own Today is served, and it is B's.
    await page.goto(net.origin + '/pingpong/');
    await expectSnapshot(page, 0);
    expect(await page.content()).toContain('E2E Reader B');
    expect(await page.content()).not.toContain('E2E Reader Off');
  });

  test("switching club drops the old club's copies", async ({ page, net }) => {
    await login(page, net, READER);
    const match = await readerMatchPath(page, net);
    await visit(page, net, match);
    const [before] = await snapshotCaches(page);

    // The drawer's club switcher: a plain POST, then a redirect.
    await Promise.all([
      page.waitForNavigation(),
      page.evaluate(() =>
        document
          .querySelector('#mobile-menu-panel [data-club-switcher] button:not([aria-current])')
          .closest('form')
          .requestSubmit()
      ),
    ]);
    await visit(page, net, '/pingpong/');
    const after = await snapshotCaches(page);
    expect(after).toHaveLength(1);
    expect(after).not.toContain(before);

    net.setMode('offline');
    await page.goto(net.origin + match);
    await expectOfflinePage(page);
    await page.goto(net.origin + '/pingpong/');
    await expectSnapshot(page, 0);
  });

  test('the kill switch: flag off, one online load, no snapshot cache', async ({ page, net }) => {
    await login(page, net, READER_OFF);
    // Off: the server opts nothing in, so nothing is stored...
    await page.goto(net.origin + '/pingpong/');
    expect(await page.locator('meta[name="ttstats-snapshots"]').getAttribute('content')).toBe('off');
    // ...and a copy left from when it was on (planted under this very
    // identity and build) is deleted by the next page load.
    await page.evaluate(async () => {
      const staticName = (await caches.keys()).find((n) => n.startsWith('ttstats-static-'));
      const build = staticName.slice('ttstats-static-'.length);
      const identity = document.querySelector('meta[name="ttstats-identity"]').content;
      const cache = await caches.open(`ttstats-snap-${build}-${identity}`);
      const html = document.documentElement.outerHTML;
      await cache.put(location.origin + '/pingpong/', new Response(html, {
        headers: { 'Content-Type': 'text/html', 'X-Snapshot-At': new Date().toISOString() },
      }));
    });
    expect(await snapshotCaches(page)).toHaveLength(1);

    await page.reload();
    await expect.poll(() => snapshotCaches(page)).toEqual([]);

    net.setMode('offline');
    // Same dead network, the worker alive and answering what it may...
    await staticStillServed(page);
    // ...but no copy of the page.
    await expect(page.goto(net.origin + '/pingpong/')).rejects.toThrow();
  });

  test('the scoreboard keeps its own route and cache', async ({ page, net }) => {
    await login(page, net, READER);
    const { token, html } = await page.evaluate(async () => {
      const text = await (await fetch('/pingpong/matches/add/')).text();
      return { html: text, token: (text.match(/name="csrfmiddlewaretoken" value="([^"]+)"/) || [])[1] };
    });
    const option = (field, label) => {
      const select = html.match(new RegExp(`<select[^>]*name="${field}"[\\s\\S]*?</select>`))[0];
      return select.match(new RegExp(`<option value="(\\d+)"[^>]*>${label}</option>`))[1];
    };
    const resp = await page.request.post(net.origin + '/pingpong/matches/add/', {
      form: {
        csrfmiddlewaretoken: token,
        player1: option('player1', 'E2E Reader'),
        player2: option('player2', 'E2E Reader B'),
        player3: '', player4: '', is_double: 'False',
        date_played: '2024-01-06T12:00', location: '', match_type: 'casual',
        best_of: '3', notes: '', start_live: '1',
      },
      headers: { Origin: net.origin, Referer: net.origin + '/pingpong/matches/add/' },
      maxRedirects: 0,
    });
    const scoreboard = (resp.headers().location || '').match(/\/pingpong\/matches\/\d+\/live\//)[0];

    await page.goto(net.origin + scoreboard);
    await expect
      .poll(() =>
        page.evaluate(async () => {
          for (const name of await caches.keys()) {
            if (!name.startsWith('ttstats-live-')) continue;
            if (await (await caches.open(name)).match(location.href)) return true;
          }
          return false;
        }),
        { timeout: 10000 }
      )
      .toBe(true);
    expect(await storedPaths(page)).not.toContain(scoreboard);

    net.setMode('offline');
    await page.reload();
    // The scoreboard's copy (not started: its server picker), not a
    // snapshot, so no saved-copy banner.
    const picker = page.getByRole('heading', { name: /serving/i });
    await expect(picker.or(page.locator('[data-score="team1"]')).first()).toBeVisible();
    expect(await freshness(page)).not.toBe('snapshot');
    expect(await storedPaths(page)).not.toContain(scoreboard);
  });
});

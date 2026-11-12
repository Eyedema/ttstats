// The offline fallback page in the service worker (KAN-137 / S12.6): when
// the snapshot route shows it (no answer and no saved copy, never when the
// network answered), how it is precached, and the {type: 'list-snapshots'}
// message the page builds its list from (current partition only).
//
// The browser side is tests/e2e/offline-page.pwa.spec.js; the page's own
// grouping is tests/js/offline_page.test.mjs.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { ORIGIN, loadWorker } from './sw_harness.mjs';
import {
  DAY, ME, OTHER, PAGE, SNAP, announce, answer, controllableNetwork, navigate, setup, stored, visitOnline,
} from './sw_snapshot_fixtures.mjs';

const OFFLINE_CACHE = 'ttstats-offline-ttstats-v5-test';
const OFFLINE_HTML =
  '<!DOCTYPE html><html lang="en" data-offline-reason="network"><head>' +
  '<title>No connection - TTStats</title></head><body><main data-offline-page>offline</main></body></html>';
const tick = () => new Promise((resolve) => setImmediate(resolve));
const offlineSetup = (options = {}) => setup({ offlinePage: OFFLINE_HTML, ...options });

async function isOfflinePage(response) {
  assert.equal(response.headers.get('X-TTStats-Served-From'), 'offline-page');
  return (await response.text()).includes('data-offline-page');
}

describe('when the offline page is the answer (pure)', () => {
  const { context } = loadWorker();

  test('offlineReason: only a failed network, never an answer', () => {
    assert.equal(context.offlineReason('error', 0), 'network');
    assert.equal(context.offlineReason('offline', 0), 'network');
    assert.equal(context.offlineReason('timeout', 0), 'network');
    for (const status of [502, 503, 504]) assert.equal(context.offlineReason('server-error', status), 'server');
    // The app itself answered: its own error page stands.
    assert.equal(context.offlineReason('server-error', 500), null);
    for (const kind of ['snapshot', 'plain', 'foreign', 'redirect', 'gone', 'client-error']) {
      assert.equal(context.offlineReason(kind, 200), null, kind);
    }
  });

  test('markOfflinePage words it for the server only when asked', () => {
    assert.equal(context.markOfflinePage(OFFLINE_HTML, 'network'), OFFLINE_HTML);
    const server = context.markOfflinePage(OFFLINE_HTML, 'server');
    assert.ok(server.includes('data-offline-reason="server"'));
    assert.ok(!server.includes('data-offline-reason="network"'));
    assert.equal(context.markOfflinePage(null, 'server'), null);
  });

  test('its cache belongs to the build', () => {
    assert.equal(context.isCurrentCache(OFFLINE_CACHE), true);
    assert.equal(context.isCurrentCache('ttstats-offline-ttstats-v4-0123456789'), false);
  });
});

describe('precaching it', () => {
  test('install fetches it with no cookies', async () => {
    const net = controllableNetwork({ offlinePage: OFFLINE_HTML });
    const worker = loadWorker({ fetch: net.fetch, timers: 'manual' });
    const lifetime = [];
    worker.listeners.install({ waitUntil: (p) => lifetime.push(p) });
    await Promise.all(lifetime);
    assert.equal(net.offlineFetches, 1);
    assert.equal(net.offlineInit.credentials, 'omit');
    const hit = await worker.storage.caches.get(OFFLINE_CACHE).match(ORIGIN + '/offline/');
    assert.ok((await hit.text()).includes('data-offline-page'));
  });

  test('missed at install, an online page load fetches it; then never again', async () => {
    const { net, worker } = await offlineSetup();
    assert.equal(net.offlineFetches, 1);
    await announce(worker, { type: 'identity', identity: ME, snapshots: 'on' });
    await announce(worker, { type: 'identity', identity: '', snapshots: 'off' });
    assert.equal(net.offlineFetches, 1);
  });

  test('an error answer is not kept', async () => {
    const { net, worker } = await setup(); // its /offline/ is a 404
    const hit = await worker.storage.caches.get(OFFLINE_CACHE).match(ORIGIN + '/offline/');
    assert.equal(hit, undefined);
    await announce(worker, { type: 'identity', identity: ME, snapshots: 'on' });
    assert.equal(net.offlineFetches, 2); // retried
  });
});

describe('the offline page in the snapshot route', () => {
  test('no answer and no copy: the offline page', async () => {
    const { net, worker } = await offlineSetup();
    const event = await navigate(worker, '/pingpong/calendar/');
    net.reject(new TypeError('Load failed'));
    assert.ok(await isOfflinePage(await event.response));
  });

  test('a page the server never opts in gets it too (it is not a copy of it)', async () => {
    const { net, worker } = await offlineSetup();
    const event = await navigate(worker, '/pingpong/matches/add/');
    net.reject(new TypeError('Load failed'));
    const html = await (await event.response).text();
    assert.ok(html.includes('data-offline-page'));
    assert.ok(!html.includes('<form'));
  });

  test('no answer but a copy: the copy, never the offline page', async () => {
    const { net, worker } = await offlineSetup();
    await visitOnline(worker, net, '/pingpong/', 'saved');
    const event = await navigate(worker, '/pingpong/');
    net.reject(new TypeError('Load failed'));
    assert.equal((await event.response).headers.get('X-TTStats-Served-From'), 'snapshot');
  });

  test("the proxy's 502/503/504 with no copy: the offline page, worded for the server", async () => {
    for (const status of [502, 503, 504]) {
      const { net, worker } = await offlineSetup();
      const event = await navigate(worker, '/pingpong/calendar/');
      net.resolve(answer('Bad gateway', { status, snapshot: false, identity: '' }));
      const response = await event.response;
      assert.equal(response.headers.get('X-TTStats-Served-From'), 'offline-page', String(status));
      assert.ok((await response.text()).includes('data-offline-reason="server"'), String(status));
    }
  });

  test('whenever the network answered, its answer stands', async () => {
    const cases = [
      () => answer(PAGE('live')),
      () => answer(PAGE('plain'), { snapshot: false, identity: '' }),
      () => answer('err', { status: 500, snapshot: false, identity: '' }),
      () => answer('nf', { status: 404, snapshot: false, identity: '' }),
      () => answer('no', { status: 403, snapshot: false, identity: '' }),
      () => new Response(null, { status: 302, headers: { Location: '/accounts/login/' } }),
    ];
    for (const make of cases) {
      const { net, worker } = await offlineSetup();
      const sent = make();
      const event = await navigate(worker, '/pingpong/calendar/');
      net.resolve(sent);
      const got = await event.response;
      assert.equal(got.status, sent.status);
      assert.equal(got.headers.get('X-TTStats-Served-From'), null);
    }
  });

  test('slow, no copy: it waits past NAV_TIMEOUT_MS, and a late answer wins', async () => {
    const { net, worker } = await offlineSetup();
    const event = await navigate(worker, '/pingpong/calendar/');
    worker.fireTimers(); // NAV_TIMEOUT_MS
    await tick();
    net.resolve(answer(PAGE('slow')));
    assert.ok((await (await event.response).text()).includes('<main>slow</main>'));
  });

  test('dead, no copy: the offline page at NAV_GIVE_UP_MS', async () => {
    const { worker } = await offlineSetup();
    const event = await navigate(worker, '/pingpong/calendar/');
    worker.fireTimers(); // NAV_TIMEOUT_MS
    for (let i = 0; i < 5; i++) await tick();
    worker.fireTimers(); // NAV_GIVE_UP_MS
    assert.ok(await isOfflinePage(await event.response));
  });

  test('the phone says it is offline: the offline page at once', async () => {
    const { worker } = await offlineSetup({ onLine: false });
    const event = await navigate(worker, '/pingpong/calendar/');
    worker.fireTimers(); // the zero-delay timer
    assert.ok(await isOfflinePage(await event.response));
  });

  test('never stored as a snapshot, and not cacheable', async () => {
    const { net, worker } = await offlineSetup();
    const event = await navigate(worker, '/pingpong/calendar/');
    net.reject(new TypeError('Load failed'));
    const response = await event.response;
    await event.settled();
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.equal(await stored(worker, ME, '/pingpong/calendar/'), null);
  });

  test("no snapshot partition (flag off, logged out): the browser's error, as before", async () => {
    for (const message of [
      { type: 'identity', identity: ME, snapshots: 'off' },
      { type: 'identity', identity: '', snapshots: 'off' },
    ]) {
      const { net, worker } = await offlineSetup();
      await announce(worker, message);
      assert.ok(worker.storage.caches.has(OFFLINE_CACHE)); // precached all the same
      const event = await navigate(worker, '/pingpong/calendar/');
      net.reject(new TypeError('Load failed'));
      await assert.rejects(event.response, undefined, JSON.stringify(message));
    }
  });

  test("not precached: the browser's error stands", async () => {
    const { net, worker } = await setup(); // its /offline/ is a 404
    const event = await navigate(worker, '/pingpong/calendar/');
    net.reject(new TypeError('Load failed'));
    await assert.rejects(event.response);
  });
});

describe('list-snapshots', () => {
  async function list(worker) {
    const replies = [];
    const lifetime = [];
    worker.listeners.message({
      data: { type: 'list-snapshots' },
      ports: [{ postMessage: (message) => replies.push(message) }],
      waitUntil: (p) => lifetime.push(p),
    });
    await Promise.all(lifetime);
    assert.equal(replies.length, 1);
    return JSON.parse(JSON.stringify(replies[0]));
  }

  test('the current partition, newest first, with title and time', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'a');
    worker.clock.now += 60 * 1000;
    await visitOnline(worker, net, '/pingpong/leaderboard/?season=3', 'b');
    const reply = await list(worker);
    assert.equal(reply.type, 'snapshots');
    assert.deepEqual(
      reply.snapshots.map((s) => s.url),
      [ORIGIN + '/pingpong/leaderboard/?season=3', ORIGIN + '/pingpong/']
    );
    assert.equal(reply.snapshots[0].title, 'Today & more · TTStats');
    assert.equal(reply.snapshots[1].savedAt, new Date(Date.UTC(2026, 10, 4, 13, 32)).toISOString());
    assert.match(reply.snapshots[0].shown, /^\d\d:\d\d$/);
    assert.deepEqual(reply.scoreboards, []);
  });

  test('nothing from another partition, even a copy planted under its name', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'mine');
    await announce(worker, { type: 'identity', identity: OTHER, snapshots: 'on' });
    await (await worker.storage.open(SNAP(ME))).put(
      ORIGIN + '/pingpong/',
      answer(PAGE('planted'), { headers: { 'X-Snapshot-At': new Date(worker.clock.now).toISOString() } })
    );
    assert.deepEqual((await list(worker)).snapshots, []);
  });

  test('no partition (logged out, or the flag off): nothing', async () => {
    for (const message of [
      { type: 'identity', identity: ME, snapshots: 'off' },
      { type: 'identity', identity: '', snapshots: 'off' },
      { type: 'purge' },
    ]) {
      const { net, worker } = await setup();
      await visitOnline(worker, net, '/pingpong/', 'mine');
      await announce(worker, message);
      assert.deepEqual((await list(worker)).snapshots, [], JSON.stringify(message));
    }
  });

  test('a copy too old to serve is not listed', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'old');
    worker.clock.now += 8 * DAY;
    assert.deepEqual((await list(worker)).snapshots, []);
  });

  test('stored scoreboards are listed with their title; stale ones are not', async () => {
    const { worker } = await setup();
    const cache = await worker.storage.open(`ttstats-live-ttstats-v5-test-${ME}`);
    const put = (pk, at) =>
      cache.put(
        ORIGIN + `/pingpong/matches/${pk}/live/`,
        new Response(`<title>Live ${pk} - TTStats</title>`, {
          headers: { 'X-TTStats-Stored-At': String(at) },
        })
      );
    await put(7, worker.clock.now);
    await put(8, worker.clock.now - 8 * DAY);
    const reply = await list(worker);
    assert.deepEqual(
      reply.scoreboards.map((s) => [s.url, s.title]),
      [[ORIGIN + '/pingpong/matches/7/live/', 'Live 7 - TTStats']]
    );
  });

  test('without a port it answers the page itself', async () => {
    const { worker } = await setup();
    const replies = [];
    const lifetime = [];
    worker.listeners.message({
      data: { type: 'list-snapshots' },
      source: { postMessage: (message) => replies.push(message) },
      waitUntil: (p) => lifetime.push(p),
    });
    await Promise.all(lifetime);
    assert.equal(replies[0].type, 'snapshots');
  });
});

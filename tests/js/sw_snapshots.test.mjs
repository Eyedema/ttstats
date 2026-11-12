// Offline snapshots in the service worker (KAN-133 / KAN-143 / KAN-144 /
// KAN-145): which requests the route claims, what each network answer does,
// partitions, LRU + age bookkeeping and the freshness rewrite.
//
// The pure helpers are called directly; the route itself is driven through
// fake fetch events, Cache Storage and timers (sw_harness.mjs). The real
// browser behaviour is tests/e2e/offline-snapshots.pwa.spec.js.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { ORIGIN, fetchEvent, loadWorker, messageEvent } from './sw_harness.mjs';

const ME = '0123456789abcdef';
const OTHER = 'fedcba9876543210';
const SNAP = (identity) => `ttstats-snap-ttstats-v4-test-${identity}`;
const DAY = 24 * 60 * 60 * 1000;

const PAGE = (text) =>
  '<!DOCTYPE html><html lang="en" data-freshness="network" data-rendered-at="x">' +
  `<head><title>Today &amp; more · TTStats</title></head><body>` +
  '<div class="snapshot-banner"><span class="snapshot-recent">No connection — saved copy from </span>' +
  `<time data-snapshot-time></time></div><main>${text}</main></body></html>`;

function answer(body, { status = 200, snapshot = true, identity = ME, headers = {} } = {}) {
  const all = { 'Content-Type': 'text/html; charset=utf-8', ...headers };
  if (snapshot) all['X-TTStats-Snapshot'] = '1';
  if (identity) all['X-TTStats-Identity'] = identity;
  return new Response(body, { status, headers: all });
}

/** A network the test answers by hand: `net.next` resolves/rejects the
 *  current request; until then it hangs. */
function controllableNetwork() {
  const net = { calls: 0 };
  net.fetch = () => {
    net.calls += 1;
    return new Promise((resolve, reject) => {
      net.resolve = resolve;
      net.reject = reject;
    });
  };
  return net;
}

async function announce(worker, data) {
  const event = messageEvent(data);
  worker.listeners.message(event);
  await event.settled();
}

async function navigate(worker, path, options) {
  const event = fetchEvent(path, options);
  worker.listeners.fetch(event);
  return event;
}

/** Visit `path` online once so a copy of `text` is stored. */
async function visitOnline(worker, net, path, text, options = {}) {
  const event = await navigate(worker, path);
  net.resolve(answer(PAGE(text), options));
  const response = await event.response;
  await event.settled();
  return response;
}

async function stored(worker, identity, path) {
  const cache = worker.storage.caches.get(SNAP(identity));
  if (!cache) return null;
  const hit = await cache.match(ORIGIN + path);
  return hit ? hit.text() : null;
}

async function setup(options = {}) {
  const net = controllableNetwork();
  const worker = loadWorker({ fetch: net.fetch, timers: 'manual', ...options });
  await announce(worker, { type: 'identity', identity: ME, snapshots: 'on' });
  return { net, worker };
}

/* ---- the route matcher -------------------------------------------------- */

describe('which requests the snapshot route claims', () => {
  const { context, listeners } = loadWorker();
  const claims = (path, options) => {
    const event = fetchEvent(path, options);
    listeners.fetch(event);
    return event.responded;
  };
  const matches = (path, { mode = 'navigate', headers = {} } = {}) =>
    context.isSnapshotNavigation(new URL(ORIGIN + path), { mode, headers: new Headers(headers) });

  test('a navigation to an app page is claimed', () => {
    assert.equal(matches('/pingpong/'), true);
    assert.equal(matches('/pingpong/leaderboard/?season=3'), true);
    assert.equal(claims('/pingpong/matches/12/'), true);
  });

  test('POSTs never reach a route', () => {
    assert.equal(claims('/pingpong/matches/add/', { method: 'POST' }), false);
    assert.equal(claims('/pingpong/', { method: 'POST' }), false);
  });

  test('htmx requests are never claimed, even as navigations', () => {
    assert.equal(claims('/pingpong/leaderboard/', { mode: 'cors', headers: { 'HX-Request': 'true' } }), false);
    assert.equal(matches('/pingpong/leaderboard/', { headers: { 'HX-Request': 'true' } }), false);
  });

  test('fetch() of a page (prefetch) is never claimed', () => {
    assert.equal(claims('/pingpong/leaderboard/', { mode: 'cors', headers: { Purpose: 'prefetch' } }), false);
    assert.equal(claims('/pingpong/', { mode: 'same-origin' }), false);
    assert.equal(claims('/pingpong/', { mode: 'no-cors' }), false);
  });

  test('JSON endpoints and /live/ are never claimed', () => {
    assert.equal(claims('/pingpong/matches/7/live/state/'), false);
    assert.equal(claims('/pingpong/matches/7/live/state/', { mode: 'navigate' }), false);
    assert.equal(matches('/pingpong/matches/7/live/sync/'), false);
    assert.equal(claims('/pingpong/api/players/', { mode: 'cors' }), false);
  });

  test('outside /pingpong/: static, admin, accounts, the worker itself', () => {
    for (const path of ['/static/pingpong/css/app.css', '/admin/', '/accounts/login/', '/sw.js', '/', '/webauthn/x/']) {
      assert.equal(matches(path), false, path);
    }
    assert.equal(claims('/accounts/login/'), false);
    assert.equal(claims('/admin/pingpong/match/'), false);
    assert.equal(claims('/static/pingpong/css/app.css', { mode: 'no-cors' }), false);
  });

  test('the scoreboard is not the snapshot route\'s (scoreboardShell is first)', () => {
    assert.equal(matches('/pingpong/matches/7/live/'), false);
    assert.equal(claims('/pingpong/matches/7/live/'), true);
  });

  test('the key keeps the query and drops the hash', () => {
    assert.equal(
      context.snapshotKey(new URL(ORIGIN + '/pingpong/leaderboard/?season=3#top')),
      ORIGIN + '/pingpong/leaderboard/?season=3'
    );
  });
});

/* ---- the decision table ------------------------------------------------- */

describe('classifying a network answer', () => {
  const { context } = loadWorker();
  const kind = (response, partition = ME) => context.classifyAnswer(response, partition);

  test('nothing at all is an error', () => {
    assert.equal(kind(null), 'error');
  });
  test('an opted-in 200 for this partition is a snapshot', () => {
    assert.equal(kind(answer('x')), 'snapshot');
  });
  test('an opted-in 200 for another identity is foreign', () => {
    assert.equal(kind(answer('x', { identity: OTHER })), 'foreign');
    assert.equal(kind(answer('x'), null), 'foreign');
    assert.equal(kind(answer('x', { identity: '' })), 'foreign');
  });
  test('a 200 without the opt-in is plain', () => {
    assert.equal(kind(answer('x', { snapshot: false })), 'plain');
  });
  test('redirects, gone and errors', () => {
    assert.equal(kind({ type: 'opaqueredirect', status: 0, headers: new Headers() }), 'redirect');
    assert.equal(kind(new Response(null, { status: 302 })), 'redirect');
    for (const status of [401, 403, 404, 410]) assert.equal(kind(answer('x', { status })), 'gone');
    assert.equal(kind(answer('x', { status: 429 })), 'client-error');
    for (const status of [500, 502, 503, 504]) assert.equal(kind(answer('x', { status })), 'server-error');
  });
});

describe('the decision table: answer kind x saved copy', () => {
  const { context } = loadWorker();
  const plan = (kind, hasCopy) => ({ ...context.planNavigation(kind, hasCopy) });
  const NET = (store, evict) => ({ respond: 'network', store, evict });

  const table = [
    ['snapshot', NET(true, false), NET(true, false)],
    ['plain', NET(false, true), NET(false, true)],
    ['foreign', NET(false, false), NET(false, false)],
    ['redirect', NET(false, true), NET(false, true)],
    ['gone', NET(false, true), NET(false, true)],
    ['client-error', NET(false, false), NET(false, false)],
    ['server-error', NET(false, false), { respond: 'copy', store: false, evict: false }],
    ['error', NET(false, false), { respond: 'copy', store: false, evict: false }],
    ['timeout', NET(false, false), { respond: 'copy', store: false, evict: false }],
    ['offline', NET(false, false), { respond: 'copy', store: false, evict: false }],
  ];
  for (const [kind, withoutCopy, withCopy] of table) {
    test(`${kind}: no copy / copy`, () => {
      assert.deepEqual(plan(kind, false), withoutCopy);
      assert.deepEqual(plan(kind, true), withCopy);
    });
  }
  test('only a failure ever serves the copy', () => {
    const serving = table.filter(([kind]) => plan(kind, true).respond === 'copy').map(([kind]) => kind);
    assert.deepEqual(serving, ['server-error', 'error', 'timeout', 'offline']);
  });
});

/* ---- bookkeeping -------------------------------------------------------- */

describe('age and LRU bookkeeping', () => {
  const { context } = loadWorker();
  const now = Date.UTC(2026, 10, 12, 12);

  test('fresh up to seven days, never from the future or unparseable', () => {
    assert.equal(context.isFresh(now - 7 * DAY, now), true);
    assert.equal(context.isFresh(now - 7 * DAY - 1, now), false);
    assert.equal(context.isFresh(now + 10 * 60000, now), false);
    assert.equal(context.isFresh(NaN, now), false);
  });

  test('expired entries go, then the least recently used beyond the cap', () => {
    const entries = [
      { key: 'old', savedAt: now - 8 * DAY },
      { key: 'a', savedAt: now - DAY },
      { key: 'b', savedAt: now - 2 * DAY },
      { key: 'broken', savedAt: NaN },
      { key: 'c', savedAt: now },
    ];
    assert.deepEqual([...context.snapshotEvictions(entries, now, 2)], ['old', 'broken', 'a']);
    assert.deepEqual([...context.snapshotEvictions(entries, now, 60)], ['old', 'broken']);
  });

  test('sixty-one fresh entries lose exactly the oldest', () => {
    const entries = Array.from({ length: 61 }, (_, i) => ({ key: `k${i}`, savedAt: now }));
    assert.deepEqual([...context.snapshotEvictions(entries, now, 60)], ['k0']);
  });

  test('the partition an identity message selects', () => {
    assert.equal(context.partitionFromMessage({ identity: ME, snapshots: 'on' }), ME);
    assert.equal(context.partitionFromMessage({ identity: ME, snapshots: 'off' }), null);
    assert.equal(context.partitionFromMessage({ identity: ME }), null); // old offline.js
    assert.equal(context.partitionFromMessage({ identity: '', snapshots: 'on' }), null);
    assert.equal(context.partitionFromMessage({ identity: '../x', snapshots: 'on' }), null);
  });

  test('the title is kept, decoded', () => {
    assert.equal(context.extractTitle(PAGE('x')), 'Today & more · TTStats');
    assert.equal(context.extractTitle('<p>no title</p>'), '');
  });
});

/* ---- the freshness rewrite --------------------------------------------- */

describe('the freshness rewrite and the banner time', () => {
  const { context } = loadWorker();
  // Local time, as the phone shows it: the test is time-zone independent.
  const at = (d, h, m) => new Date(2026, 10, d, h, m).getTime();

  test('same day: the clock time', () => {
    assert.deepEqual({ ...context.formatSavedAt(at(4, 14, 32), at(4, 14, 44)) }, { text: '14:32', old: false });
    assert.deepEqual({ ...context.formatSavedAt(at(4, 9, 5), at(4, 23, 0)) }, { text: '09:05', old: false });
  });

  test('yesterday, under 24 h: the weekday and clock', () => {
    assert.deepEqual({ ...context.formatSavedAt(at(3, 23, 5), at(4, 1, 0)) }, { text: 'Tue 23:05', old: false });
  });

  test('over 24 h: the date, and the stronger wording', () => {
    assert.deepEqual({ ...context.formatSavedAt(at(4, 14, 32), at(6, 9, 0)) }, { text: 'Wed 4 Nov', old: true });
  });

  test('the marker and the time are rewritten, once each', () => {
    const saved = at(4, 14, 32);
    const html = context.markSnapshot(PAGE('data-freshness="network" in text'), saved, at(4, 14, 40));
    const iso = new Date(saved).toISOString();
    assert.ok(html.includes(`<html lang="en" data-freshness="snapshot" data-snapshot-at="${iso}" data-snapshot-age="recent" data-rendered-at="x">`));
    assert.ok(html.includes(`<time data-snapshot-time datetime="${iso}">14:32</time>`));
    // Only the first occurrence: the root element's.
    assert.ok(html.includes('<main>data-freshness="network" in text</main>'));
    assert.equal(html.includes('<time data-snapshot-time></time>'), false);
  });

  test('an old copy is marked old', () => {
    const html = context.markSnapshot(PAGE('x'), at(1, 8, 0), at(4, 9, 0));
    assert.ok(html.includes('data-snapshot-age="old"'));
    assert.ok(html.includes('>Sun 1 Nov</time>'));
  });

  test('a page with "$" patterns is copied literally', () => {
    const html = context.markSnapshot(PAGE("$& $' $1"), at(4, 1, 0), at(4, 2, 0));
    assert.ok(html.includes("<main>$& $' $1</main>"));
  });

  test('no marker, or no time placeholder: no copy', () => {
    assert.equal(context.markSnapshot('<html><body><time data-snapshot-time></time>', 0, 1), null);
    assert.equal(context.markSnapshot('<html data-freshness="network"><body>', 0, 1), null);
    assert.equal(context.markSnapshot(undefined, 0, 1), null);
  });
});

/* ---- the route, end to end in the vm ----------------------------------- */

describe('the snapshot route', () => {
  test('online: the network page is returned untouched and a copy stored', async () => {
    const { net, worker } = await setup();
    const response = await visitOnline(worker, net, '/pingpong/', 'live');
    assert.equal(response.headers.get('X-TTStats-Served-From'), null);
    assert.ok((await response.text()).includes('data-freshness="network"'));
    assert.ok((await stored(worker, ME, '/pingpong/')).includes('<main>live</main>'));
  });

  test('online never serves stale: a newer network page wins over the copy', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'old');
    const response = await visitOnline(worker, net, '/pingpong/', 'new');
    const html = await response.text();
    assert.ok(html.includes('<main>new</main>'));
    assert.equal(response.headers.get('X-TTStats-Served-From'), null);
    assert.ok((await stored(worker, ME, '/pingpong/')).includes('<main>new</main>'));
  });

  test('a 5xx serves the labelled copy, with the page\'s own headers', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'saved', {
      headers: { 'Content-Security-Policy': "default-src 'self'" },
    });
    const event = await navigate(worker, '/pingpong/');
    net.resolve(answer('Bad gateway', { status: 502, snapshot: false, identity: '' }));
    const response = await event.response;
    const html = await response.text();
    assert.ok(html.includes('<main>saved</main>'));
    assert.ok(html.includes('data-freshness="snapshot"'));
    assert.equal(response.headers.get('X-TTStats-Served-From'), 'snapshot');
    assert.equal(response.headers.get('Content-Security-Policy'), "default-src 'self'");
  });

  test('a 5xx with no copy is the 5xx', async () => {
    const { net, worker } = await setup();
    const event = await navigate(worker, '/pingpong/');
    net.resolve(answer('Bad gateway', { status: 502, snapshot: false, identity: '' }));
    assert.equal((await event.response).status, 502);
  });

  test('a network error serves the copy; with no copy it stays an error', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'saved');
    let event = await navigate(worker, '/pingpong/');
    net.reject(new TypeError('Load failed'));
    assert.ok((await (await event.response).text()).includes('<main>saved</main>'));

    event = await navigate(worker, '/pingpong/leaderboard/');
    net.reject(new TypeError('Load failed'));
    await assert.rejects(event.response);
  });

  test('a timeout serves the copy; the late answer is stored and announced', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'saved');
    const event = await navigate(worker, '/pingpong/');
    worker.fireTimers();
    assert.ok((await (await event.response).text()).includes('<main>saved</main>'));
    assert.equal(worker.messages.length, 0);

    net.resolve(answer(PAGE('late')));
    await event.settled();
    assert.ok((await stored(worker, ME, '/pingpong/')).includes('<main>late</main>'));
    assert.deepEqual(JSON.parse(JSON.stringify(worker.messages)), [
      { id: 'client-1', message: { type: 'snapshot-fresh', url: ORIGIN + '/pingpong/' } },
    ]);
  });

  test('a timeout with no copy keeps waiting for the network', async () => {
    const { net, worker } = await setup();
    const event = await navigate(worker, '/pingpong/');
    worker.fireTimers();
    net.resolve(answer(PAGE('slow')));
    assert.ok((await (await event.response).text()).includes('<main>slow</main>'));
    await event.settled();
    assert.equal(worker.messages.length, 0); // nothing stale was shown
  });

  test('navigator.onLine false skips the wait', async () => {
    const { net, worker } = await setup({ onLine: false });
    // Stored while "offline" too: onLine is only a hint.
    await visitOnline(worker, net, '/pingpong/', 'saved');
    const event = await navigate(worker, '/pingpong/');
    worker.fireTimers(); // the zero-delay timer
    assert.ok((await (await event.response).text()).includes('data-freshness="snapshot"'));
  });

  test('a redirect (login) is returned as is and evicts the copy', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'saved');
    const event = await navigate(worker, '/pingpong/');
    net.resolve(new Response(null, { status: 302, headers: { Location: '/accounts/login/' } }));
    assert.equal((await event.response).status, 302);
    await event.settled();
    assert.equal(await stored(worker, ME, '/pingpong/'), null);
  });

  test('a 404 evicts; a 2xx no longer opted in evicts; a 429 keeps it', async () => {
    const { net, worker } = await setup();
    for (const [status, snapshot, kept] of [[429, false, true], [404, false, false]]) {
      await visitOnline(worker, net, '/pingpong/matches/1/', 'saved');
      const event = await navigate(worker, '/pingpong/matches/1/');
      net.resolve(answer('x', { status, snapshot }));
      assert.equal((await event.response).status, status);
      await event.settled();
      assert.equal((await stored(worker, ME, '/pingpong/matches/1/')) !== null, kept, String(status));
    }
    await visitOnline(worker, net, '/pingpong/', 'saved');
    await visitOnline(worker, net, '/pingpong/', 'plain', { snapshot: false });
    assert.equal(await stored(worker, ME, '/pingpong/'), null);
  });

  test('never stores another identity\'s page', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'theirs', { identity: OTHER });
    assert.equal(await stored(worker, ME, '/pingpong/'), null);
    assert.equal(worker.storage.caches.has(SNAP(OTHER)), false);
  });

  test('nothing is stored before a page announces a partition', async () => {
    const net = controllableNetwork();
    const worker = loadWorker({ fetch: net.fetch, timers: 'manual' });
    await visitOnline(worker, net, '/pingpong/', 'x');
    assert.deepEqual(await worker.storage.keys(), []);
  });

  test('a page without the markers is not stored (no label, no copy)', async () => {
    const { net, worker } = await setup();
    const event = await navigate(worker, '/pingpong/');
    net.resolve(answer('<html><body>no marker</body></html>'));
    await event.response;
    await event.settled();
    assert.equal(await stored(worker, ME, '/pingpong/'), null);
  });

  test('a copy past seven days is deleted, never served', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'ancient');
    worker.clock.now += 7 * DAY + 1;
    const event = await navigate(worker, '/pingpong/');
    net.resolve(answer('down', { status: 503, snapshot: false }));
    assert.equal((await event.response).status, 503);
    assert.equal(await stored(worker, ME, '/pingpong/'), null);
  });

  test('serving a copy makes it the most recently used', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/a/', 'a');
    await visitOnline(worker, net, '/pingpong/b/', 'b');
    const event = await navigate(worker, '/pingpong/a/');
    net.reject(new TypeError('offline'));
    await event.response;
    await event.settled();
    const keys = (await worker.storage.caches.get(SNAP(ME)).keys()).map((k) => k.url);
    assert.deepEqual(keys, [ORIGIN + '/pingpong/b/', ORIGIN + '/pingpong/a/']);
  });

  test('the stored copy carries its time and title, not the encoding', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'x', {
      headers: { 'Content-Encoding': 'gzip', 'Content-Length': '12' },
    });
    const hit = await worker.storage.caches.get(SNAP(ME)).match(ORIGIN + '/pingpong/');
    assert.equal(hit.headers.get('X-Snapshot-At'), new Date(worker.clock.now).toISOString());
    assert.equal(decodeURIComponent(hit.headers.get('X-Snapshot-Title')), 'Today & more · TTStats');
    assert.equal(hit.headers.get('Content-Encoding'), null);
    assert.equal(hit.headers.get('Content-Length'), null);
  });
});

/* ---- partitions (the data-leak boundary) -------------------------------- */

describe('snapshot partitions', () => {
  test('another identity deletes this one\'s copies; its own come back empty', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'mine');
    await announce(worker, { type: 'identity', identity: OTHER, snapshots: 'on' });
    assert.equal(worker.storage.caches.has(SNAP(ME)), false);

    // B offline: nothing of A's is served.
    const event = await navigate(worker, '/pingpong/');
    net.reject(new TypeError('offline'));
    await assert.rejects(event.response);
  });

  test('logged out ("") and the kill switch (off) delete every snapshot', async () => {
    for (const message of [
      { type: 'identity', identity: '', snapshots: 'off' },
      { type: 'identity', identity: ME, snapshots: 'off' },
      { type: 'identity', identity: ME }, // an offline.js from before KAN-133
      { type: 'purge' },
    ]) {
      const { net, worker } = await setup();
      await visitOnline(worker, net, '/pingpong/', 'mine');
      await announce(worker, message);
      const names = (await worker.storage.keys()).filter((name) => name.startsWith('ttstats-snap-'));
      assert.deepEqual(names, [], JSON.stringify(message));
      // ...and nothing is stored again until the flag is on again.
      await visitOnline(worker, net, '/pingpong/', 'again');
      assert.equal(await stored(worker, ME, '/pingpong/'), null, JSON.stringify(message));
    }
  });

  test('a store racing an identity change is dropped', async () => {
    const { net, worker } = await setup();
    const event = await navigate(worker, '/pingpong/');
    const announced = announce(worker, { type: 'identity', identity: OTHER, snapshots: 'on' });
    net.resolve(answer(PAGE('mine')));
    await event.response;
    await announced;
    await event.settled();
    assert.equal(worker.storage.caches.has(SNAP(ME)), false);
  });

  test('the partition survives the worker being restarted', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'mine');
    // A fresh worker over the same Cache Storage: no message yet.
    const net2 = controllableNetwork();
    const reloaded = loadWorker({ fetch: net2.fetch, timers: 'manual', storage: worker.storage });
    const event = await navigate(reloaded, '/pingpong/');
    net2.reject(new TypeError('offline'));
    assert.ok((await (await event.response).text()).includes('<main>mine</main>'));
  });

  test('scoreboard partitions keep their own rule (snapshots flag irrelevant)', async () => {
    const { worker } = await setup();
    await worker.storage.open(`ttstats-live-ttstats-v4-test-${ME}`);
    await announce(worker, { type: 'identity', identity: ME, snapshots: 'off' });
    assert.equal(worker.storage.caches.has(`ttstats-live-ttstats-v4-test-${ME}`), true);
  });
});

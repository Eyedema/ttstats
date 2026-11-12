// Background warm-up in the service worker (KAN-136 / S12.5): which URLs
// are fetched, one at a time, with what request; what is stored (the
// navigation route's own rules); the 30-minute rate limit per partition; and
// the run stopping when the identity changes or the network goes.
//
// The page side is tests/js/snapshot_warm.test.mjs; the browser,
// tests/e2e/offline-warm.pwa.spec.js.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { ORIGIN, loadWorker } from './sw_harness.mjs';
import { ME, OTHER, PAGE, announce, answer, controllableNetwork, setup, stored, visitOnline } from './sw_snapshot_fixtures.mjs';

const MIN = 60 * 1000;
const tick = () => new Promise((resolve) => setImmediate(resolve));

async function callsReach(net, n) {
  for (let i = 0; i < 100 && net.calls < n; i++) await tick();
  assert.equal(net.calls, n, `expected ${n} fetches`);
}

function startWarm(worker, urls) {
  const replies = [];
  const lifetime = [];
  worker.listeners.message({
    data: { type: 'warm', urls },
    ports: [{ postMessage: (message) => replies.push(message) }],
    waitUntil: (p) => lifetime.push(p),
  });
  return async () => {
    await Promise.all(lifetime);
    return JSON.parse(JSON.stringify(replies[0]));
  };
}

/** Warm `paths`, answering each fetch with a stored-able page. */
async function warmAll(worker, net, paths) {
  const done = startWarm(worker, paths);
  const before = net.calls;
  for (let i = 0; i < paths.length; i++) {
    await callsReach(net, before + i + 1);
    net.resolve(answer(PAGE(`warm ${i}`)));
  }
  return done();
}

describe('warmTargets (pure)', () => {
  const { context } = loadWorker();
  const targets = (urls) => [...context.warmTargets(urls, ORIGIN)];

  test('same-origin pages the snapshot route would claim, as keys', () => {
    assert.deepEqual(targets(['/pingpong/', ORIGIN + '/pingpong/leaderboard/']), [
      ORIGIN + '/pingpong/',
      ORIGIN + '/pingpong/leaderboard/',
    ]);
  });
  test('never another origin, nothing outside /pingpong/, nothing live', () => {
    assert.deepEqual(
      targets([
        'https://evil.test/pingpong/',
        '//evil.test/pingpong/',
        '/admin/',
        '/accounts/logout/',
        '/sw.js',
        '/pingpong/matches/3/live/',
        '/pingpong/live/lobby/2/',
        'javascript:alert(1)',
        42,
        null,
      ]),
      []
    );
  });
  test('query kept, #hash dropped, each once, at most six', () => {
    assert.deepEqual(targets(['/pingpong/leaderboard/?season=2#x', '/pingpong/leaderboard/?season=2']), [
      ORIGIN + '/pingpong/leaderboard/?season=2',
    ]);
    const many = Array.from({ length: 10 }, (_, i) => `/pingpong/matches/${i}/`);
    assert.equal(targets(many).length, 6);
  });
  test('not a list: nothing', () => {
    assert.deepEqual(targets('/pingpong/'), []);
    assert.deepEqual(targets(undefined), []);
  });
});

describe('warmDue (pure)', () => {
  const { context } = loadWorker();
  const now = Date.UTC(2026, 10, 4, 13, 32);
  test('no partition: never', () => assert.equal(context.warmDue(null, null, now), false));
  test('never warmed, or warmed for another partition: yes', () => {
    assert.equal(context.warmDue(null, ME, now), true);
    assert.equal(context.warmDue({ partition: OTHER, at: now }, ME, now), true);
  });
  test('once per 30 minutes', () => {
    assert.equal(context.warmDue({ partition: ME, at: now - 29 * MIN }, ME, now), false);
    assert.equal(context.warmDue({ partition: ME, at: now - 30 * MIN }, ME, now), true);
  });
  test('a timestamp from the future (clock moved back) does not block it', () => {
    assert.equal(context.warmDue({ partition: ME, at: now + 60 * MIN }, ME, now), true);
    assert.equal(context.warmDue({ partition: ME, at: 'x' }, ME, now), true);
  });
});

describe('a warm run', () => {
  test('fetches one at a time, low priority, marked, with cookies; stores each', async () => {
    const { net, worker } = await setup();
    const done = startWarm(worker, ['/pingpong/', '/pingpong/leaderboard/', '/pingpong/players/1/']);
    for (let i = 1; i <= 3; i++) {
      await callsReach(net, i);
      for (let j = 0; j < 5; j++) await tick();
      assert.equal(net.calls, i, 'the next fetch waits for this one');
      net.resolve(answer(PAGE(`page ${i}`)));
    }
    const reply = await done();
    assert.deepEqual(reply, {
      type: 'warmed',
      ran: true,
      reason: 'done',
      stored: [ORIGIN + '/pingpong/', ORIGIN + '/pingpong/leaderboard/', ORIGIN + '/pingpong/players/1/'],
    });
    for (const { request, init } of net.requests) {
      assert.equal(typeof request, 'string');
      assert.equal(init.credentials, 'same-origin');
      assert.equal(init.priority, 'low');
      assert.equal(init.cache, 'no-store');
      assert.equal(init.headers['X-TTStats-Warm'], '1');
    }
    assert.ok((await stored(worker, ME, '/pingpong/players/1/')).includes('page 3'));
  });

  test('stores only by the navigation rules: opted in, this identity', async () => {
    const { net, worker } = await setup();
    const done = startWarm(worker, ['/pingpong/a/', '/pingpong/b/', '/pingpong/c/']);
    await callsReach(net, 1);
    net.resolve(answer(PAGE('not opted in'), { snapshot: false, identity: '' }));
    await callsReach(net, 2);
    net.resolve(answer('no markers')); // opted in, but unlabelable
    await callsReach(net, 3);
    net.resolve(answer('gone', { status: 404, snapshot: false, identity: '' }));
    const reply = await done();
    assert.deepEqual(reply.stored, []);
    for (const path of ['/pingpong/a/', '/pingpong/b/', '/pingpong/c/']) {
      assert.equal(await stored(worker, ME, path), null, path);
    }
  });

  test('a 404 evicts an old copy of that page', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/players/9/', 'old');
    worker.clock.now += 20 * MIN; // past WARM_SKIP_FRESH_MS
    const done = startWarm(worker, ['/pingpong/players/9/']);
    await callsReach(net, 2);
    net.resolve(answer('gone', { status: 404, snapshot: false, identity: '' }));
    await done();
    assert.equal(await stored(worker, ME, '/pingpong/players/9/'), null);
  });

  test('at most once per 30 minutes per partition, surviving a restart', async () => {
    const { net, worker } = await setup();
    await warmAll(worker, net, ['/pingpong/']);
    assert.equal(net.calls, 1);

    worker.clock.now += 29 * MIN;
    const again = await startWarm(worker, ['/pingpong/leaderboard/'])();
    assert.deepEqual(again, { type: 'warmed', ran: false, reason: 'rate-limited', stored: [] });
    assert.equal(net.calls, 1);

    // A restarted worker over the same caches remembers.
    const net2 = controllableNetwork();
    const restarted = loadWorker({ fetch: net2.fetch, timers: 'manual', storage: worker.storage });
    restarted.clock.now = worker.clock.now;
    assert.equal((await startWarm(restarted, ['/pingpong/leaderboard/'])()).reason, 'rate-limited');
    assert.equal(net2.calls, 0);

    worker.clock.now += 1 * MIN; // 30 minutes after the first run
    const later = await warmAll(worker, net, ['/pingpong/leaderboard/']);
    assert.equal(later.ran, true);
    assert.equal(net.calls, 2);
  });

  test('another identity is a new partition: it may warm at once', async () => {
    const { net, worker } = await setup();
    await warmAll(worker, net, ['/pingpong/']);
    await announce(worker, { type: 'identity', identity: OTHER, snapshots: 'on' });
    const done = startWarm(worker, ['/pingpong/']);
    await callsReach(net, 2);
    net.resolve(answer(PAGE('other'), { identity: OTHER }));
    assert.equal((await done()).stored.length, 1);
  });

  test('the identity changing mid-run stops it; nothing stored for anyone', async () => {
    const { net, worker } = await setup();
    const done = startWarm(worker, ['/pingpong/', '/pingpong/leaderboard/', '/pingpong/players/1/']);
    await callsReach(net, 1);
    await announce(worker, { type: 'identity', identity: OTHER, snapshots: 'on' });
    net.resolve(answer(PAGE('mine')));
    const reply = await done();
    assert.equal(reply.reason, 'partition changed');
    assert.deepEqual(reply.stored, []);
    assert.equal(net.calls, 1);
    assert.equal(await stored(worker, ME, '/pingpong/'), null);
    assert.equal(await stored(worker, OTHER, '/pingpong/'), null);
  });

  test("another identity's page (club switched in another tab) stops it", async () => {
    const { net, worker } = await setup();
    const done = startWarm(worker, ['/pingpong/', '/pingpong/leaderboard/']);
    await callsReach(net, 1);
    net.resolve(answer(PAGE('foreign'), { identity: OTHER }));
    const reply = await done();
    assert.equal(reply.reason, 'foreign');
    assert.equal(net.calls, 1);
  });

  for (const [label, response, reason] of [
    ['a redirect (session gone)', () => new Response(null, { status: 302 }), 'redirect'],
    ['a 5xx', () => answer('down', { status: 502, snapshot: false, identity: '' }), 'server-error'],
  ]) {
    test(`${label} stops it`, async () => {
      const { net, worker } = await setup();
      const done = startWarm(worker, ['/pingpong/', '/pingpong/leaderboard/']);
      await callsReach(net, 1);
      net.resolve(response());
      assert.equal((await done()).reason, reason);
      assert.equal(net.calls, 1);
    });
  }

  test('no network stops it, and the attempt still counts against the limit', async () => {
    const { net, worker } = await setup();
    const done = startWarm(worker, ['/pingpong/', '/pingpong/leaderboard/']);
    await callsReach(net, 1);
    net.reject(new TypeError('Load failed'));
    assert.equal((await done()).reason, 'network');
    assert.equal((await startWarm(worker, ['/pingpong/'])()).reason, 'rate-limited');
  });

  test('a page saved minutes ago is not fetched again', async () => {
    const { net, worker } = await setup();
    await visitOnline(worker, net, '/pingpong/', 'just now');
    worker.clock.now += 5 * MIN;
    const done = startWarm(worker, ['/pingpong/', '/pingpong/leaderboard/']);
    await callsReach(net, 2);
    assert.equal(net.requests[1].request, ORIGIN + '/pingpong/leaderboard/');
    net.resolve(answer(PAGE('table')));
    assert.deepEqual((await done()).stored, [ORIGIN + '/pingpong/leaderboard/']);
  });

  test('no partition (flag off, logged out): nothing fetched', async () => {
    const { net, worker } = await setup();
    await announce(worker, { type: 'identity', identity: ME, snapshots: 'off' });
    const reply = await startWarm(worker, ['/pingpong/'])();
    assert.equal(reply.ran, false);
    assert.equal(net.calls, 0);
  });

  test('two tabs asking at once share one run', async () => {
    const { net, worker } = await setup();
    const first = startWarm(worker, ['/pingpong/']);
    const second = startWarm(worker, ['/pingpong/']);
    await callsReach(net, 1);
    net.resolve(answer(PAGE('once')));
    assert.deepEqual(await first(), await second());
    for (let j = 0; j < 5; j++) await tick();
    assert.equal(net.calls, 1);
  });
});

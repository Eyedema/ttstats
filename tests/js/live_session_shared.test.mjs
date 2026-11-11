// KAN-153 / KAN-166: two phones, one match. Two LiveSessions against one
// FakeServer (per-stream seqs, rev, side restriction, expects, long-poll),
// on one fake clock.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import {
  CONFIRM_URL, FakeServer, fakeClock, loadSession, makeSession, memoryStorage, networkDown, rules,
} from './live_fakes.mjs';

const LiveSession = loadSession();
const ABORT_MS = 25000; // what scoreboard-config.longpoll_abort_ms carries in the tests' server

function table({ bestOf = 3, initialServer = 'team1' } = {}) {
  const clock = fakeClock();
  const server = FakeServer.shared(bestOf, { initialServer, clock });
  const phone = (as, extra = {}) => makeSession({
    server, clock, as, shared: true, longpollAbortMs: ABORT_MS,
    userId: as === 'host' ? 1 : 2, storage: memoryStorage(), ...extra,
  });
  return { clock, server, phone };
}

const point = (side) => ({ type: 'point', side });
const tap = (phone, side, n = 1) => {
  for (let i = 0; i < n; i += 1) assert.equal(phone.session.dispatch(point(side)).ok, true);
};
const types = (p) => p.events.map((e) => e.type);
const shown = (p) => LiveSession.comparable(p.session.view().state);
const truth = (server) => LiveSession.comparable(server.state);

describe('shared: two phones converge', () => {
  test('interleaved taps converge to the same state on both', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    const order = ['team1', 'team2', 'team2', 'team1', 'team1', 'team2', 'team1'];
    for (const side of order) {
      tap(side === 'team1' ? host : guest, side);
      await clock.advance(60); // inside the debounce: the batches overlap
    }
    await clock.advance(2000);
    assert.equal(server.log.length, order.length);
    assert.equal(shown(host), truth(server));
    assert.equal(shown(guest), truth(server));
    assert.equal(host.session.view().pending, 0);
    assert.equal(guest.session.view().pending, 0);
    for (const p of [host, guest]) {
      assert.deepEqual(types(p), []);
      assert.ok(!p.reports.some((r) => /diverged|another device/.test(r.message)));
    }
  });

  test('the other phone\'s points arrive by long-poll, without any tap here', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    tap(guest, 'team2', 3);
    await clock.advance(200);
    assert.equal(host.session.view().state.team2_points, 3);
    assert.equal(host.transport.calls, 0); // the host never flushed
    assert.ok(server.polls.some((p) => p.as === 'host' && p.wait));
  });

  test('one phone offline for a whole game converges with every op applied once', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    guest.transport.online = false;
    tap(guest, 'team2', 11); // a whole game, on this phone only
    tap(host, 'team1', 4);
    await clock.advance(60000);
    assert.equal(guest.session.view().state.team2_games, 1);
    assert.equal(guest.session.view().pending, 11);
    // The guest's phone already shows the host's 4 points (polled before going
    // dark? no: offline). The host sees none of the guest's.
    assert.equal(host.session.view().state.team2_points, 0);

    guest.transport.online = true;
    await guest.session.nudge();
    await clock.advance(5000);
    assert.equal(server.log.length, 15);
    assert.equal(server.log.filter((p) => p.side === 'team2').length, 11);
    assert.equal(shown(host), truth(server));
    assert.equal(shown(guest), truth(server));
    assert.equal(server.state.team2_games, 1); // 11-4, arrival order
    for (const p of [host, guest]) {
      assert.ok(!types(p).includes('divergence'));
      assert.ok(!types(p).includes('conflict'));
    }
  });

  test('no "server wins" from the other stream, even mid-flight', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    let release;
    host.transport.script.push((srv, ops) => new Promise((resolve) => {
      release = () => resolve(srv.sync(ops, { as: 'host' }));
    }));
    tap(host, 'team1');
    await clock.advance(150); // the host's sync is in flight...
    tap(guest, 'team2', 2);
    await clock.advance(150); // ...and the guest's points land first
    release();
    await clock.advance(300);
    assert.deepEqual(types(host), []);
    assert.ok(!host.reports.some((r) => /diverged/.test(r.message)));
    assert.equal(shown(host), truth(server));
  });

  test('the server still wins against my own prediction when nobody else moved', async () => {
    const clock = fakeClock();
    const server = FakeServer.shared(3, { clock });
    const host = makeSession({ server, clock, as: 'host', shared: true, longpollAbortMs: ABORT_MS, storage: memoryStorage() });
    server.tamper = (op) => (op.type === 'start' ? { ...op, initial_server: 'team2' } : op);
    host.session.dispatch({ type: 'start', initial_server: 'team1' });
    await clock.advance(200);
    assert.deepEqual(types(host), ['divergence']);
    assert.equal(host.session.view().state.initial_server, 'team2');
  });
});

describe('shared: follow mode is only for the same user', () => {
  test('the other scorer never puts a phone in follow mode', async () => {
    const { clock, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    tap(guest, 'team2', 5);
    tap(host, 'team1', 5);
    await clock.advance(3000);
    assert.notEqual(host.session.view().phase, 'follow');
    assert.notEqual(guest.session.view().phase, 'follow');
  });

  test('the same user on two devices: the second follows', async () => {
    const { clock, server, phone } = table();
    const first = phone('host');
    const second = phone('host'); // same scorer, another phone (own storage)
    await clock.advance(10);
    tap(first, 'team1', 3);
    // One held poll per user: the second phone's is `busy` and retries ~1 s on.
    await clock.advance(1500);
    assert.equal(second.session.view().phase, 'follow');
    assert.ok(types(second).includes('conflict'));
    assert.equal(first.session.view().phase, 'synced');
    assert.equal(second.session.view().state.team1_points, 3);
    assert.equal(server.log.length, 3);
  });

  test('...and taps on the stale device are taken as duplicates, then follow', async () => {
    const { clock, phone } = table();
    const first = phone('host');
    const second = phone('host', { isVisible: () => false }); // no polling
    tap(first, 'team1', 3);
    await clock.advance(400);
    tap(second, 'team1');
    await clock.advance(400);
    assert.equal(second.session.view().phase, 'follow');
  });
});

describe('shared: the long-poll loop', () => {
  test('a held long-poll never delays a flush', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    // The load-time wait=0 answered; a wait=1 poll is now held.
    assert.equal(server.held.size, 1);
    tap(host, 'team1');
    await clock.advance(149);
    assert.deepEqual(server.requests, []);
    await clock.advance(1);
    assert.deepEqual(server.requests, [[1]]); // sent on the debounce, poll or not
    assert.equal(host.session.view().pending, 0);
  });

  test('exactly one poll in flight; wait=0 on load, then wait=1, re-issued on 200 and 204', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    assert.deepEqual(host.transport.pollCalls.map((c) => c.wait), [false, true]);
    // The server's hold ends: 204. KAN-154: one plain read (no `since`) for
    // the other phone's presence, then held again at once.
    await clock.advance(20000);
    assert.deepEqual(host.transport.pollCalls.map((c) => c.wait), [false, true, false, true]);
    assert.equal(host.transport.pollCalls[2].since, null);
    server.sync([{ seq: 1, type: 'point', side: 'team2' }], { as: 'guest' });
    await clock.advance(1);
    assert.equal(host.transport.pollCalls.length, 5);
    assert.equal(host.transport.pollCalls[4].since, 1);
    assert.equal(server.held.size, 1);
  });

  test('a held poll is abandoned after longpoll_abort_ms and retried with backoff', async () => {
    const { clock, phone } = table();
    const host = phone('host', { random: () => 0 });
    await clock.advance(10);
    host.transport.pollScript.push(() => new Promise(() => {})); // never answers
    host.transport.pollScript.push(networkDown, networkDown);
    host.session.nudge(); // wait=0, held forever by the script
    const before = host.transport.pollCalls.length;
    await clock.advance(8000); // instant polls give up after the flush timeout
    assert.equal(host.transport.pollCalls.length, before);
    await clock.advance(800); // backoff 1 s - 20%
    assert.equal(host.transport.pollCalls.length, before + 1);
    await clock.advance(800); // second failure: 2 s - 20%
    assert.equal(host.transport.pollCalls.length, before + 1);
    await clock.advance(800);
    assert.equal(host.transport.pollCalls.length, before + 2);
  });

  test('busy: back off about a second instead of spinning', async () => {
    const { clock, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    const before = host.transport.pollCalls.length;
    host.session.nudge(); // aborts the held one client-side; the server still holds its slot
    await clock.advance(10);
    // wait=0 (200 or 204) then wait=1 -> busy: the slot is the aborted poll's.
    const after = host.transport.pollCalls.length;
    assert.equal(after, before + 2);
    await clock.advance(500);
    assert.equal(host.transport.pollCalls.length, after);
    await clock.advance(600);
    assert.equal(host.transport.pollCalls.length, after + 1);
  });

  test('hidden: nothing in flight; visible again starts with wait=0', async () => {
    const { clock, server, phone } = table();
    let visible = true;
    const host = phone('host', { isVisible: () => visible });
    await clock.advance(10);
    visible = false;
    host.session.pausePolling();
    const count = host.transport.pollCalls.length;
    server.sync([{ seq: 1, type: 'point', side: 'team2' }], { as: 'guest' });
    await clock.advance(60000);
    assert.equal(host.transport.pollCalls.length, count);
    visible = true;
    host.session.nudge();
    await clock.advance(10);
    assert.equal(host.transport.pollCalls[count].wait, false);
    assert.equal(host.session.view().state.team2_points, 1);
  });

  test('a single-scorer session never polls', async () => {
    const clock = fakeClock();
    const server = new FakeServer(3, { initialServer: 'team1', clock });
    const solo = makeSession({ server, clock, longpollAbortMs: ABORT_MS });
    tap(solo, 'team1', 3);
    await clock.advance(1000);
    await solo.session.nudge();
    await clock.advance(30000);
    assert.equal(solo.transport.pollCalls.length, 0);
    assert.equal(server.polls.length, 0);
  });

  test('completion learnt from the long-poll navigates to the server\'s redirect', async () => {
    const { clock, server, phone } = table({ bestOf: 1 });
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    tap(host, 'team1', 11);
    await clock.advance(1000);
    assert.equal(server.live, false);
    assert.deepEqual(types(guest), ['completed']);
    assert.equal(guest.events[0].detail.redirectUrl, CONFIRM_URL);
    assert.deepEqual(types(host), ['completed']);
    const polls = guest.transport.pollCalls.length;
    await clock.advance(60000);
    assert.equal(guest.transport.pollCalls.length, polls); // the loop stopped
  });

  test('the match ended while my points were queued: they are sent, the server decides', async () => {
    const { clock, server, phone } = table({ bestOf: 1 });
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    guest.transport.script.push(networkDown);
    tap(guest, 'team2');
    await clock.advance(150);
    tap(host, 'team1', 11);
    await clock.advance(500);
    // The poll told the guest; its queued point went to the server, which
    // refused it ("match complete") and handed over the redirect.
    assert.ok(server.requests.length >= 2);
    assert.deepEqual(types(guest), ['completed']);
    assert.equal(server.log.length, 11);
  });
});

describe('shared: start, undo, stuck points', () => {
  test('two starts: the loser rebases to the winner\'s server, no toast', async () => {
    const clock = fakeClock();
    const server = FakeServer.shared(3, { clock });
    const host = makeSession({ server, clock, as: 'host', shared: true, longpollAbortMs: ABORT_MS, storage: memoryStorage() });
    const guest = makeSession({ server, clock, as: 'guest', shared: true, longpollAbortMs: ABORT_MS, userId: 2, storage: memoryStorage() });
    host.transport.online = false;
    host.session.dispatch({ type: 'start', initial_server: 'team1' });
    guest.session.dispatch({ type: 'start', initial_server: 'team2' });
    await clock.advance(300);
    host.transport.online = true;
    await host.session.nudge();
    await clock.advance(300);
    assert.equal(server.state.initial_server, 'team2');
    assert.equal(host.session.view().state.initial_server, 'team2');
    assert.deepEqual(types(host), []);
    assert.equal(host.session.view().pending, 0);
  });

  test('undo carries expects = the trailing point this phone shows', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    phone('guest');
    await clock.advance(10);
    tap(host, 'team1', 2);
    await clock.advance(300);
    assert.equal(host.session.view().trailingSeq, 2);
    const result = host.session.dispatch({ type: 'undo' });
    assert.equal(result.op.expects, 2);
    await clock.advance(300);
    assert.equal(server.log.length, 1);
    assert.deepEqual(types(host), []);
  });

  test('undo of my own still-queued point predicts its sequence', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    phone('guest');
    await clock.advance(10);
    server.sync([{ seq: 1, type: 'point', side: 'team2' }], { as: 'guest' });
    await clock.advance(10); // the host knows point #1
    tap(host, 'team1');
    const undo = host.session.dispatch({ type: 'undo' });
    assert.equal(undo.op.expects, 2); // my queued point will be #2
    assert.equal(host.session.view().state.team1_points, 0);
    await clock.advance(300);
    assert.deepEqual(server.log.map((p) => p.side), ['team2']);
    assert.deepEqual(types(host), []);
  });

  test('a point landing before my undo: stale, nothing of theirs deleted, the score converges', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    host.transport.online = false;
    tap(host, 'team1');
    host.session.dispatch({ type: 'undo' }); // expects 1: my point, if nothing lands first
    tap(guest, 'team2'); // ...but the guest's point is #1
    await clock.advance(300);
    host.transport.online = true;
    await host.session.nudge();
    await clock.advance(300);
    assert.deepEqual(server.log.map((p) => p.side), ['team2', 'team1']);
    assert.deepEqual(types(host), ['stale-undo']);
    assert.equal(shown(host), truth(server));
    assert.equal(shown(guest), truth(server));
  });

  test('nothing to undo: refused locally, nothing queued', () => {
    const { phone } = table();
    const host = phone('host');
    assert.deepEqual(host.session.dispatch({ type: 'undo' }), { ok: false, error: 'Nothing to undo' });
    assert.equal(host.outbox.size(), 0);
  });

  test('queued points the new base makes impossible are kept and counted as stuck', async () => {
    const { clock, server, phone } = table({ bestOf: 1 });
    const guest = phone('guest');
    guest.transport.online = false;
    tap(guest, 'team2');
    // A base whose game is over but which is still live (as far as the
    // mirror can tell): replaying the queued point throws.
    let state = server.state;
    for (let i = 0; i < 11; i += 1) state = rules.applyPoint(state, 'team1')[0];
    const fresh = phone('guest', {
      storage: guest.storage, bootstrap: { ...server.payload({}, 'guest'), state, rev: 11 },
    });
    assert.equal(fresh.session.view().stuck, 1);
    assert.equal(fresh.session.view().pending, 1);
    await clock.advance(10);
  });

  test('a gap that does not resolve renumbers my ops instead of following', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    phone('guest');
    await clock.advance(10);
    host.outbox.append(point('team1'), 4); // seq 5: 1..4 lost from storage
    host.session.nudge();
    await clock.advance(0);
    await clock.advance(1500);
    assert.notEqual(host.session.view().phase, 'follow');
    assert.equal(server.log.length, 1);
    assert.equal(server.scorer('host').applied, 1);
    assert.ok(host.reports.some((r) => /renumbered/.test(r.message)));
  });
});

describe('fetchTransport.poll', () => {
  test('builds ?since=&wait=1, reads X-Longpoll: busy, and a 204 is not a login bounce', async () => {
    const calls = [];
    const fetch = (url) => {
      calls.push(url);
      return Promise.resolve({
        status: 204, ok: true, redirected: false,
        headers: { get: (h) => (h.toLowerCase() === 'x-longpoll' ? 'busy' : 'text/html; charset=utf-8') },
      });
    };
    const transport = LiveSession.fetchTransport({ fetch, syncUrl: '/s/', stateUrl: '/state/' });
    const resp = await transport.poll({ since: 7, wait: true });
    assert.deepEqual(calls, ['/state/?since=7&wait=1']);
    assert.equal(resp.status, 204);
    assert.equal(resp.busy, true);
    assert.equal(resp.loginRedirect, false);
  });
});

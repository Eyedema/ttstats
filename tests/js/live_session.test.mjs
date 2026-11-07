// KAN-94: the flush loop and reconcile, against a fake live_sync.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import {
  FakeServer, answer, hang, loadSession, loginBounce, lostAck, makeSession,
  memoryStorage, networkDown, rules,
} from './live_fakes.mjs';

const started = (bestOf = 3) => new FakeServer(bestOf, { initialServer: 'team1' });
const LiveSession = loadSession();
const point = (side) => ({ type: 'point', side });

function tapPoints(session, sides) {
  for (const side of sides) assert.equal(session.dispatch(point(side)).ok, true);
}

describe('LiveSession: local state', () => {
  test('a tap is applied locally at once and persisted before any request', () => {
    const { session, transport, outbox } = makeSession({ server: started() });
    tapPoints(session, ['team1']);
    assert.equal(session.view().state.team1_points, 1);
    assert.equal(outbox.size(), 1);
    assert.equal(transport.calls, 0);
  });

  test('a full game rolls over offline: server indicator, games, deuce all from the mirror', async () => {
    const { session, transport, clock } = makeSession({ server: started() });
    transport.sync = () => Promise.reject(new TypeError('offline'));
    tapPoints(session, Array(10).fill('team1').concat(Array(10).fill('team2')));
    const deuce = session.view();
    assert.equal(deuce.state.team1_points, 10);
    assert.equal(deuce.currentServer, rules.currentServer(deuce.state));
    tapPoints(session, ['team1', 'team1']);
    const v = session.view();
    assert.equal(v.state.team1_games, 1);
    assert.equal(v.state.current_game_number, 2);
    assert.deepEqual(v.games, [{ game_number: 1, team1: 12, team2: 10 }]);
    await clock.advance(100);
  });

  test('a refused op is not stored', () => {
    const { session, outbox } = makeSession({ server: new FakeServer(3) });
    const result = session.dispatch(point('team1'));
    assert.equal(result.ok, false);
    assert.match(result.error, /not started/);
    assert.equal(outbox.size(), 0);
  });
});

describe('LiveSession: flushing', () => {
  test('taps within the debounce coalesce into one request', async () => {
    const { session, server, clock } = makeSession({ server: started() });
    tapPoints(session, ['team1', 'team2', 'team1']);
    await clock.advance(149);
    assert.equal(server.requests.length, 0);
    await clock.advance(1);
    assert.deepEqual(server.requests, [[1, 2, 3]]);
    assert.equal(session.view().pending, 0);
    assert.equal(session.view().phase, 'synced');
  });

  test('single flight: taps during a request go in the next one, never in parallel', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server });
    let release;
    transport.script.push((srv, ops) => new Promise((resolve) => {
      release = () => resolve(srv.sync(ops));
    }));
    tapPoints(session, ['team1']);
    await clock.advance(150);
    tapPoints(session, ['team1', 'team1']);
    await clock.advance(1000);
    assert.equal(transport.calls, 1);
    assert.equal(session.view().state.team1_points, 3); // never backwards
    release();
    await clock.advance(150);
    assert.deepEqual(server.requests, [[1], [2, 3]]);
    assert.equal(server.state.team1_points, 3);
  });

  test('a lost ack is re-sent and deduped: nothing double-counts', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server });
    transport.script.push(lostAck);
    tapPoints(session, ['team1', 'team1']);
    await clock.advance(150);
    assert.equal(server.points.length, 2);
    assert.equal(session.view().pending, 2);
    assert.equal(session.view().phase, 'offline');
    await clock.advance(1200); // first backoff step (1 s +/- 20%)
    assert.equal(server.points.length, 2);
    assert.equal(server.state.team1_points, 2);
    assert.equal(session.view().pending, 0);
    assert.equal(session.view().state.team1_points, 2);
  });

  test('backoff steps 1/2/5/10/30 s with jitter while failing, reset on success', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server, random: () => 0 });
    for (let i = 0; i < 6; i += 1) transport.script.push(networkDown);
    tapPoints(session, ['team1']);
    await clock.advance(150);
    const gaps = [];
    for (let i = 0; i < 6; i += 1) {
      gaps.push(clock.pending()[0]);
      await clock.advance(clock.pending()[0]);
    }
    // random() = 0 is the low end of the +/-20% jitter.
    assert.deepEqual(gaps, [800, 1600, 4000, 8000, 24000, 24000]);
    assert.equal(session.view().pending, 0);
  });

  test('a hung request is aborted after the timeout and retried', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server, timeoutMs: 8000 });
    transport.script.push(hang);
    tapPoints(session, ['team2']);
    await clock.advance(150 + 7999);
    assert.equal(session.view().pending, 1);
    await clock.advance(1);
    assert.equal(session.view().phase, 'offline');
    await clock.advance(1200);
    assert.equal(server.state.team2_points, 1);
    assert.equal(session.view().pending, 0);
  });

  test('the timeout frees the queue even when the transport ignores the signal', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server, makeController: () => null });
    transport.script.push(() => new Promise(() => {}));
    tapPoints(session, ['team1']);
    await clock.advance(150 + 8000 + 1200);
    assert.equal(server.state.team1_points, 1);
  });

  test('a 5xx keeps the ops and backs off', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server });
    transport.script.push(answer(502));
    tapPoints(session, ['team1']);
    await clock.advance(150);
    assert.equal(session.view().pending, 1);
    await clock.advance(1200);
    assert.equal(session.view().pending, 0);
  });

  test('nudge() (online / visible / Retry) flushes at once, skipping the backoff', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server });
    transport.script.push(networkDown);
    tapPoints(session, ['team1']);
    await clock.advance(150);
    await session.nudge();
    await clock.advance(0);
    assert.equal(session.view().pending, 0);
  });

  test('a batch never exceeds maxBatch ops', async () => {
    const server = started();
    const { session, clock } = makeSession({ server, maxBatch: 2 });
    tapPoints(session, ['team1', 'team1', 'team1']);
    await clock.advance(1000);
    assert.deepEqual(server.requests, [[1, 2], [3]]);
  });
});

describe('LiveSession: reconcile', () => {
  test('reload: pending ops are replayed onto the bootstrap and sync once', async () => {
    const server = started();
    const storage = memoryStorage();
    const first = makeSession({ server, storage });
    first.transport.sync = () => Promise.reject(new TypeError('offline'));
    tapPoints(first.session, Array(12).fill('team1'));
    first.session.stop();

    const second = makeSession({ server, storage, bootstrap: server.payload() });
    assert.equal(second.session.view().state.team1_games, 1);
    assert.equal(second.session.view().state.team1_points, 1);
    assert.equal(second.session.view().pending, 12);
    await second.clock.advance(10);
    assert.deepEqual(server.requests, [[...Array(12).keys()].map((i) => i + 1)]);
    assert.equal(server.points.length, 12);
  });

  test('a stale bootstrap loses to the newer stored snapshot', async () => {
    const server = started();
    const storage = memoryStorage();
    const stale = server.payload(); // what a cached page would carry
    const first = makeSession({ server, storage });
    tapPoints(first.session, ['team1', 'team1', 'team1']);
    await first.clock.advance(150); // acknowledged; outbox empty
    first.transport.sync = () => Promise.reject(new TypeError('offline'));
    tapPoints(first.session, ['team2']);
    first.session.stop();

    const second = makeSession({ server, storage, bootstrap: stale });
    const v = second.session.view();
    assert.equal(v.state.team1_points, 3);
    assert.equal(v.state.team2_points, 1);
    assert.equal(v.appliedSeq, 3);
  });

  test('a rejected op is dropped, the score re-derived from the server, and reported', async () => {
    const server = started();
    server.forceReject.set(2, 'Invalid side');
    const { session, clock, events, reports } = makeSession({ server });
    tapPoints(session, ['team1', 'team1', 'team2']);
    await clock.advance(150);
    // Seq 2 consumed its seq; seq 3 is still pending and goes next.
    assert.deepEqual(events.map((e) => e.type), ['rejected']);
    assert.equal(events[0].detail.seq, 2);
    assert.ok(reports.some((r) => /rejected/.test(r.message)));
    assert.equal(session.view().state.team1_points, 1);
    assert.equal(session.view().state.team2_points, 1);
    await clock.advance(150);
    assert.deepEqual(server.requests, [[1, 2, 3], [3]]);
    assert.equal(server.state.team2_points, 1);
    assert.equal(session.view().pending, 0);
  });

  test('server state != predicted: the server wins and the drift is reported', async () => {
    const server = started();
    server.tamper = (op) => (op.type === 'point' ? { ...op, side: 'team2' } : op);
    const { session, clock, events, reports } = makeSession({ server });
    tapPoints(session, ['team1']);
    await clock.advance(150);
    assert.deepEqual(events.map((e) => e.type), ['divergence']);
    assert.ok(reports.some((r) => /diverged/.test(r.message)));
    assert.equal(session.view().state.team1_points, 0);
    assert.equal(session.view().state.team2_points, 1);
  });

  test('a 409 gap is retried once, then the tab goes read-only', async () => {
    const server = started();
    server.applied = 0;
    const storage = memoryStorage();
    const { session, clock, events, outbox } = makeSession({ server, storage });
    // Simulate an outbox whose early ops vanished: only seq 5 is pending.
    outbox.append(point('team1'), 4);
    session.nudge();
    await clock.advance(0);
    assert.deepEqual(events, []); // first strike: one more try
    await clock.advance(1200);
    assert.deepEqual(events.map((e) => e.type), ['conflict']);
    assert.equal(session.view().phase, 'follow');
    assert.equal(session.dispatch(point('team1')).ok, false);
  });

  test('applied_seq ahead of anything this device issued: another device is scoring', async () => {
    const server = started();
    const { session, clock, events } = makeSession({ server });
    // Another phone scored seqs 1..3 meanwhile.
    server.sync([1, 2, 3].map((seq) => ({ seq, type: 'point', side: 'team2' })));
    tapPoints(session, ['team1']);
    await clock.advance(150);
    assert.deepEqual(events.map((e) => e.type), ['conflict']);
    assert.equal(session.view().phase, 'follow');
    assert.equal(session.view().state.team2_points, 3);
    await session.takeOver();
    assert.equal(session.view().phase, 'synced');
    assert.equal(session.dispatch(point('team1')).op.seq, 4);
  });

  test('403 keeps the outbox, stops the loop, and Retry after re-auth syncs', async () => {
    const server = started();
    const { session, clock, transport, events } = makeSession({ server });
    transport.script.push(answer(403, { detail: 'CSRF' }));
    tapPoints(session, ['team1', 'team2']);
    await clock.advance(150);
    assert.deepEqual(events.map((e) => e.type), ['auth']);
    assert.equal(session.view().phase, 'auth');
    await clock.advance(60000);
    assert.equal(transport.calls, 1); // no hammering while signed out
    assert.equal(session.view().pending, 2);
    await session.nudge();
    await clock.advance(0);
    assert.equal(server.points.length, 2);
  });

  test('a redirect to the login page is an auth failure, not a success', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server });
    transport.script.push(loginBounce);
    tapPoints(session, ['team1']);
    await clock.advance(150);
    assert.equal(session.view().phase, 'auth');
    assert.equal(session.view().pending, 1);
  });

  test('completion is shown locally, and acknowledged only by the server', async () => {
    const server = started(3);
    const { session, clock, transport, events, storage } = makeSession({ server });
    transport.script.push(networkDown);
    tapPoints(session, Array(22).fill('team1'));
    const v = session.view();
    assert.equal(v.complete, true);
    assert.equal(v.winner, 'team1');
    assert.deepEqual(v.games.map((g) => [g.team1, g.team2]), [[11, 0], [11, 0]]);
    assert.equal(session.dispatch(point('team2')).ok, false);
    await clock.advance(150);
    assert.deepEqual(events, []);
    await clock.advance(1200);
    assert.deepEqual(events.map((e) => e.type), ['completed']);
    assert.equal(events[0].detail.redirectUrl, '/pingpong/match/1/confirm/');
    assert.equal(storage.data.size, 0);
  });

  test('undo of an unsynced point is just another op', async () => {
    const server = started();
    const { session, clock } = makeSession({ server });
    tapPoints(session, ['team1']);
    session.dispatch({ type: 'undo' });
    assert.equal(session.view().state.team1_points, 0);
    await clock.advance(150);
    assert.deepEqual(server.requests, [[1, 2]]);
    assert.equal(server.points.length, 0);
  });

  test('refresh() adopts the server only when nothing is pending', async () => {
    const server = started();
    const { session, clock, transport } = makeSession({ server });
    transport.sync = () => Promise.reject(new TypeError('offline'));
    server.state = rules.applyPoint(server.state, 'team2')[0];
    tapPoints(session, ['team1']);
    await session.refresh();
    assert.equal(session.view().state.team2_points, 0);
    await clock.advance(10);
  });
});

describe('LiveSession: comparable()', () => {
  test('ignores timestamps but not the score or the event sides', () => {
    const start = rules.setInitialServer(rules.initialState(3), 'team1');
    const a = rules.applyPoint(start, 'team1', { now: '2026-01-01T00:00:00Z' })[0];
    const b = rules.applyPoint(start, 'team1', { now: '2026-01-02T00:00:00Z' })[0];
    const c = rules.applyPoint(start, 'team2', { now: '2026-01-01T00:00:00Z' })[0];
    assert.notDeepEqual(a, b);
    assert.equal(LiveSession.comparable(a), LiveSession.comparable(b));
    assert.notEqual(LiveSession.comparable(a), LiveSession.comparable(c));
  });
});

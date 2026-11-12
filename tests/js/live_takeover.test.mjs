// KAN-154: presence, takeover and recovery, in the client.
//
//   - presenceLine: the strip's words, driven by last_seen_at in the payload
//     and the bootstrap's thresholds (never typed in JS);
//   - take_over / release: ops in my stream, the layout flag at the tap;
//   - undo names its recorder (expects_by): the reused-sequence hazard;
//   - removed: 403 {"error": "removed"} stops every loop, keeps the outbox,
//     and is never mistaken for a stale CSRF token;
//   - a held poll that times out is followed by a fresh read (presence).
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import {
  FakeServer, fakeClock, loadSession, makeSession, memoryStorage,
} from './live_fakes.mjs';

const LiveSession = loadSession();
const ABORT_MS = 25000;
const T = { inSyncS: 10, lostS: 120 }; // the shape of scoreboard-config.presence

function table({ bestOf = 3, initialServer = 'team1' } = {}) {
  const clock = fakeClock();
  const server = FakeServer.shared(bestOf, { initialServer, clock });
  const phone = (as, extra = {}) => makeSession({
    server, clock, as, shared: true, longpollAbortMs: ABORT_MS,
    userId: as === 'host' ? 1 : 2, storage: memoryStorage(), ...extra,
  });
  return { clock, server, phone };
}

const tap = (phone, side, n = 1) => {
  for (let i = 0; i < n; i += 1) assert.equal(phone.session.dispatch({ type: 'point', side }).ok, true);
};

describe('presenceLine', () => {
  const at = (iso) => Date.parse(iso);
  const marco = (extra = {}) => ({
    name: 'Marco', last_seen_at: '2026-11-11T12:00:00+00:00', revoked: false, polling: false, ...extra,
  });
  const line = (scorer, { serverNow = '2026-11-11T12:00:05+00:00', elapsedMs = 0, ...t } = {}) => {
    const receivedAtMs = at('2026-11-11T15:00:00Z'); // the phone's clock is 3 h off: irrelevant
    return LiveSession.presenceLine(scorer, {
      nowMs: receivedAtMs + elapsedMs, receivedAtMs, serverNow, ...T, ...t,
    });
  };

  test('in sync under the first threshold', () => {
    assert.deepEqual(line(marco()), { status: 'sync', ageS: 5, text: 'Marco’s phone · in sync' });
  });

  test('last seen ... ago past it, aged on this phone\'s clock since the answer', () => {
    const got = line(marco(), { elapsedMs: 35000 });
    assert.equal(got.status, 'behind');
    assert.equal(got.text, 'Marco’s phone · last seen 40 s ago, their points will arrive when it reconnects');
  });

  test('not seen for ... past the second, in minutes', () => {
    const got = line(marco(), { serverNow: '2026-11-11T12:03:00+00:00' });
    assert.equal(got.status, 'lost');
    assert.equal(got.text, 'Marco’s phone · not seen for 3 min');
  });

  test('the thresholds are the bootstrap\'s, not constants', () => {
    assert.equal(line(marco(), { inSyncS: 3, lostS: 8 }).status, 'behind');
    assert.equal(line(marco(), { inSyncS: 2, lostS: 4 }).status, 'lost');
    assert.equal(line(marco(), { inSyncS: 6, lostS: 8 }).status, 'sync');
  });

  test('a phone holding a long-poll is in sync whatever its stamp', () => {
    const got = line(marco({ polling: true }), { serverNow: '2026-11-11T12:10:00+00:00' });
    assert.equal(got.status, 'sync');
  });

  test('never seen, scoring both sides, removed', () => {
    assert.equal(line(marco({ last_seen_at: null })).status, 'lost');
    assert.match(line(marco({ can_score_both: true })).text, /· scoring both sides$/);
    assert.equal(line(marco({ revoked: true })), null);
  });

  test('without server_now the phone\'s clock is used', () => {
    const got = LiveSession.presenceLine(marco(), {
      nowMs: at('2026-11-11T12:00:20Z'), receivedAtMs: at('2026-11-11T12:00:20Z'), serverNow: null, ...T,
    });
    assert.equal(got.status, 'behind');
  });
});

describe('take_over / release', () => {
  test('take_over is an op in my stream; the other side is mine at the tap', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    assert.equal(host.session.view().canScoreBoth, false);
    assert.equal(host.session.dispatch({ type: 'take_over' }).ok, true);
    assert.equal(host.session.view().canScoreBoth, true); // before any answer
    tap(host, 'team2', 2);
    await clock.advance(500);
    assert.deepEqual(host.events.map((e) => e.type), []);
    assert.equal(server.scorer('host').can_score_both, true);
    assert.equal(server.scorer('guest').can_score_both, false);
    assert.equal(server.scorer('guest').revoked, false);
    assert.deepEqual(server.log.map((p) => [p.side, p.by]), [['team2', 1], ['team2', 1]]);
    assert.equal(host.session.view().canScoreBoth, true);
    assert.equal(host.session.view().pending, 0);
  });

  test('taken over offline: the take_over lands before the points tapped after it', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    host.transport.online = false;
    host.session.dispatch({ type: 'take_over' });
    tap(host, 'team2', 3);
    await clock.advance(3000);
    assert.equal(server.log.length, 0);
    host.transport.online = true;
    host.session.nudge();
    await clock.advance(500);
    assert.equal(server.log.length, 3);
    assert.equal(host.events.length, 0);
  });

  test('the other phone\'s queued points still apply after a takeover', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    guest.transport.online = false;
    tap(guest, 'team2', 2);
    host.session.dispatch({ type: 'take_over' });
    tap(host, 'team2', 1);
    await clock.advance(1000);
    guest.transport.online = true;
    guest.session.nudge();
    await clock.advance(1000);
    assert.equal(server.state.team2_points, 3);
    assert.equal(guest.session.view().state.team2_points, 3);
    assert.equal(host.session.view().state.team2_points, 3);
    // Both phones learn the host scores both.
    const hostRow = guest.session.view().scorers.find((s) => s.role === 'host');
    assert.equal(hostRow.can_score_both, true);
  });

  test('release gives the side back', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    host.session.dispatch({ type: 'take_over' });
    host.session.dispatch({ type: 'release' });
    assert.equal(host.session.view().canScoreBoth, false);
    await clock.advance(500);
    assert.equal(server.scorer('host').can_score_both, false);
  });
});

describe('undo names its recorder (expects_by)', () => {
  test('an undo of the other phone\'s point names that phone', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    tap(guest, 'team2');
    await clock.advance(500);
    assert.equal(host.session.view().trailingBy, 2);
    host.session.dispatch({ type: 'undo' });
    const sent = host.outbox.pending()[0];
    assert.deepEqual([sent.expects, sent.expects_by], [1, 2]);
    await clock.advance(500);
    assert.equal(server.log.length, 0);
  });

  test('an undo of my own queued point names me', async () => {
    const { clock, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    host.transport.online = false;
    tap(host, 'team1');
    host.session.dispatch({ type: 'undo' });
    const undo = host.outbox.pending()[1];
    assert.deepEqual([undo.expects, undo.expects_by], [1, 1]);
  });

  test('the reused sequence: the other phone undid #1 and scored a new #1', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    tap(host, 'team1');
    await clock.advance(500); // both phones show the host's #1
    // The host goes dark and taps Undo: "#1, recorded by me"...
    host.transport.online = false;
    host.session.dispatch({ type: 'undo' });
    // ...while the guest undoes that #1 and scores: a new #1, the guest's.
    guest.session.dispatch({ type: 'undo' });
    await clock.advance(500);
    tap(guest, 'team2');
    await clock.advance(500);
    assert.deepEqual(server.log.map((p) => [p.sequence, p.by]), [[1, 2]]);
    // The host learns the new #1 (its poll still reads) and its queued undo
    // is skipped locally, as the server will refuse it.
    host.transport.online = true;
    await host.session.refresh();
    host.transport.online = false;
    assert.equal(host.session.view().state.team2_points, 1);
    // Sequence alone (#1) would delete the guest's point; the pair refuses.
    host.transport.online = true;
    host.session.nudge();
    await clock.advance(1000);
    assert.deepEqual(server.log.map((p) => [p.sequence, p.by]), [[1, 2]], 'the new #1 survives');
    assert.ok(host.events.some((e) => e.type === 'stale-undo'));
    assert.equal(host.session.view().state.team2_points, 1);
  });

  test('below the base\'s trailing point the recorder is unknown: expects alone', async () => {
    const { clock, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    tap(host, 'team1', 2);
    await clock.advance(500);
    host.transport.online = false;
    host.session.dispatch({ type: 'undo' });
    host.session.dispatch({ type: 'undo' });
    const [first, second] = host.outbox.pending();
    assert.equal(first.expects_by, 1);
    assert.equal(second.expects, 1);
    assert.equal(second.expects_by, undefined);
  });
});

describe('removed from the match', () => {
  test('the guest\'s next flush gets 403 removed: loops stop, outbox kept', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    const result = await host.session.removeScorer('/pingpong/matches/1/live/scorers/2/remove/');
    assert.deepEqual(result, { ok: true });
    assert.equal(host.session.view().canScoreBoth, true);
    await clock.advance(10);
    // The guest's held poll woke on the removal and re-issued: 403 removed.
    assert.equal(guest.session.view().phase, 'removed');
    assert.ok(guest.events.some((e) => e.type === 'removed'));
    assert.equal(guest.session.dispatch({ type: 'point', side: 'team2' }).ok, false);
    const guestPolls = () => server.polls.filter((p) => p.as === 'guest').length;
    const polls = guestPolls();
    await clock.advance(120000);
    assert.equal(guestPolls(), polls, 'no more polls');
    assert.equal(guest.transport.calls, 0);
  });

  test('ops queued before the removal stay in the outbox', async () => {
    const { clock, server, phone } = table();
    const guest = phone('guest');
    await clock.advance(10);
    guest.transport.online = false;
    tap(guest, 'team2', 2);
    server.remove('guest', { as: 'host' });
    guest.transport.online = true;
    guest.session.nudge();
    await clock.advance(1000);
    assert.equal(guest.session.view().phase, 'removed');
    assert.equal(guest.outbox.pending().length, 2);
    assert.equal(server.log.length, 0);
  });

  test('fetchTransport does not retry "removed" as a stale CSRF token', async () => {
    const calls = [];
    const json = (status, body) => ({
      status, ok: status < 300, redirected: false,
      headers: { get: (k) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: () => Promise.resolve(body),
    });
    const transport = LiveSession.fetchTransport({
      fetch: (url, init) => {
        calls.push([url, init.method || 'GET']);
        return Promise.resolve(json(403, { error: 'removed' }));
      },
      syncUrl: '/sync/', stateUrl: '/state/', csrfToken: 'x',
    });
    const resp = await transport.sync([{ seq: 1, type: 'point', side: 'team2' }]);
    assert.equal(resp.status, 403);
    assert.deepEqual(resp.body, { error: 'removed' });
    assert.deepEqual(calls, [['/sync/', 'POST']]);
  });

  test('a plain 403 still means signed out (auth), not removed', async () => {
    const { clock, phone } = table();
    const guest = phone('guest');
    await clock.advance(10);
    guest.transport.script.push(() => Promise.resolve({ status: 403, body: null, loginRedirect: false }));
    tap(guest, 'team2');
    await clock.advance(500);
    assert.equal(guest.session.view().phase, 'auth');
  });
});

describe('presence refresh', () => {
  test('a held poll that times out is followed by a fresh read', async () => {
    const { clock, server, phone } = table();
    const host = phone('host');
    await clock.advance(10);
    const before = host.session.view().presence.receivedAt;
    server.scorer('guest').polling = true; // what the fake reports is not modelled; the read is
    await clock.advance(20000);
    const fresh = host.transport.pollCalls.find((c) => c.since === null);
    assert.ok(fresh, 'a read without since');
    assert.ok(host.session.view().presence.receivedAt > before);
  });

  test('presence comes from every answer, newest rev wins', async () => {
    const { clock, phone } = table();
    const host = phone('host');
    const guest = phone('guest');
    await clock.advance(10);
    tap(guest, 'team2');
    await clock.advance(500);
    const p = host.session.view().presence;
    assert.equal(p.rev, 1);
    assert.equal(p.scorers.find((s) => s.role === 'guest').applied_seq, 1);
  });
});

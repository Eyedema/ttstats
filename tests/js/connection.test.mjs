// KAN-135: the connection state machine. Fake clock, fake outcomes; no DOM.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, test } from 'node:test';

const require = createRequire(import.meta.url);
const Connection = require(new URL('../../ttstats/pingpong/static/pingpong/js/connection.js', import.meta.url).pathname);

function machine({ onLine = true } = {}) {
  let t = 1_000_000;
  const clock = { now: () => t, advance: (ms) => { t += ms; } };
  const m = Connection.createMachine({ now: clock.now, onLine });
  // One request that takes `ms` and ends with `outcome`.
  let n = 0;
  m.request = (ms, outcome = 'response') => {
    const key = { n: n++ };
    m.begin(key);
    clock.advance(ms);
    return m.end(key, outcome);
  };
  return { m, clock };
}

describe('constants', () => {
  test('thresholds are the ones the story names', () => {
    assert.equal(Connection.SLOW_MS, 2500);
    assert.equal(Connection.TIMEOUT_MS, 8000);
    assert.equal(Connection.OFFLINE_AFTER, 2);
  });
});

describe('createMachine', () => {
  test('starts online, or offline when the browser says so', () => {
    assert.equal(machine().m.state(), 'online');
    assert.equal(machine({ onLine: false }).m.state(), 'offline');
  });

  test('a fast response keeps it online', () => {
    const { m } = machine();
    assert.equal(m.request(300), 'online');
  });

  test('a response at or over SLOW_MS degrades, a fast one recovers', () => {
    const { m } = machine();
    assert.equal(m.request(3000), 'degraded');
    assert.equal(m.request(2500), 'degraded');
    assert.equal(m.request(2499), 'online');
  });

  test('an HTTP error status is still a response: the network worked', () => {
    const { m } = machine();
    assert.equal(m.request(200, 'response'), 'online');
  });

  test('a timeout degrades', () => {
    const { m } = machine();
    assert.equal(m.request(8000, 'timeout'), 'degraded');
  });

  test('one network error degrades, two in a row mean offline', () => {
    const { m } = machine();
    assert.equal(m.request(10, 'network'), 'degraded');
    assert.equal(m.request(10, 'network'), 'offline');
    assert.equal(m.request(10, 'network'), 'offline');
    assert.equal(m.request(100), 'online');
  });

  test('a timeout between network errors breaks the streak', () => {
    const { m } = machine();
    m.request(10, 'network');
    m.request(8000, 'timeout');
    assert.equal(m.request(10, 'network'), 'degraded');
  });

  test('an abort is no evidence either way', () => {
    const { m } = machine();
    m.request(10, 'network');
    assert.equal(m.request(10, 'abort'), 'degraded');
    assert.equal(m.request(10, 'network'), 'offline');
  });

  test('a request still in flight past SLOW_MS degrades on tick', () => {
    const { m, clock } = machine();
    const key = {};
    m.begin(key);
    clock.advance(2000);
    assert.equal(m.tick(), 'online');
    clock.advance(600);
    assert.equal(m.tick(), 'degraded');
    assert.equal(m.inFlight(), 1);
    assert.equal(m.end(key, 'response'), 'degraded'); // 2.6 s in all
    assert.equal(m.inFlight(), 0);
  });

  test('ending the same request twice counts it once', () => {
    const { m, clock } = machine();
    const key = {};
    m.begin(key);
    clock.advance(10);
    m.end(key, 'network');
    assert.equal(m.end(key, 'network'), 'degraded');
  });

  test('navigator offline wins over everything; coming back resets', () => {
    const { m } = machine();
    m.request(3000);
    assert.equal(m.setOnLine(false), 'offline');
    assert.equal(m.request(100), 'offline');
    assert.equal(m.setOnLine(true), 'online');
  });

  test('an online event that is not a transition changes nothing', () => {
    const { m } = machine();
    m.request(3000);
    assert.equal(m.setOnLine(true), 'degraded');
  });
});

describe('messageFor', () => {
  test('network, timeout and 5xx get honest wording; 4xx gets none', () => {
    assert.equal(Connection.messageFor('network').text, 'No connection — try again');
    assert.equal(Connection.messageFor('timeout').text, 'Connection is very slow — try again');
    assert.match(Connection.messageFor('response', 502).text, /went wrong/);
    assert.equal(Connection.messageFor('response', 404), null);
    assert.equal(Connection.messageFor('response', 200), null);
  });
});

describe('isExempt', () => {
  test('everything under /live/ belongs to KAN-76', () => {
    assert.equal(Connection.isExempt('/pingpong/matches/4/live/'), true);
    assert.equal(Connection.isExempt('/pingpong/live/lobby/3/'), true);
    assert.equal(Connection.isExempt('/pingpong/live/join/abc/'), true);
    assert.equal(Connection.isExempt('/pingpong/leaderboard/'), false);
  });

  test('or a page carrying the scoreboard or lobby root', () => {
    const doc = (hit) => ({ getElementById: () => (hit === 'id' ? {} : null), querySelector: () => (hit === 'q' ? {} : null) });
    assert.equal(Connection.isExempt('/x/', doc('id')), true);
    assert.equal(Connection.isExempt('/x/', doc('q')), true);
    assert.equal(Connection.isExempt('/x/', doc(null)), false);
  });
});

// KAN-164: live_lobby.js's poll decisions and loop, without a browser.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, test } from 'node:test';

const require = createRequire(import.meta.url);
const LiveLobby = require(
  new URL('../../ttstats/pingpong/static/pingpong/js/live_lobby.js', import.meta.url).pathname,
);

describe('decide', () => {
  test('open keeps waiting', () => {
    assert.deepEqual(LiveLobby.decide({ status: 'open' }), { action: 'wait' });
  });
  test('joined navigates to the scoreboard', () => {
    assert.deepEqual(
      LiveLobby.decide({ status: 'joined', match_url: '/pingpong/matches/7/live/' }),
      { action: 'navigate', url: '/pingpong/matches/7/live/' },
    );
  });
  test('joined without a url keeps waiting', () => {
    assert.deepEqual(LiveLobby.decide({ status: 'joined' }), { action: 'wait' });
  });
  for (const status of ['expired', 'cancelled']) {
    test(`${status} ends with the reason`, () => {
      assert.deepEqual(LiveLobby.decide({ status, reason: 'Gone.' }), { action: 'ended', reason: 'Gone.' });
    });
  }
  test('garbage keeps waiting', () => {
    assert.deepEqual(LiveLobby.decide(null), { action: 'wait' });
  });
});

function harness(responses, { visible = true } = {}) {
  const timers = [];
  const verdicts = [];
  let calls = 0;
  const state = { visible };
  const poller = LiveLobby.createPoller({
    interval: 2000,
    isVisible: () => state.visible,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { timers[id - 1] = null; },
    fetchStatus: () => {
      const next = responses[Math.min(calls, responses.length - 1)];
      calls += 1;
      return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
    },
    onVerdict: (v) => verdicts.push(v),
  });
  const fire = async () => {
    const idx = timers.findLastIndex((t) => t !== null);
    const t = timers[idx];
    timers[idx] = null;
    t.fn();
    await new Promise((r) => setImmediate(r));
  };
  const pending = () => timers.filter((t) => t !== null).length;
  return { poller, timers, verdicts, fire, pending, state, calls: () => calls };
}

describe('createPoller', () => {
  test('polls every interval until joined, then stops', async () => {
    const h = harness([{ status: 'open' }, { status: 'open' }, { status: 'joined', match_url: '/m/' }]);
    h.poller.start();
    assert.equal(h.pending(), 1);
    assert.equal(h.timers[0].ms, 2000);
    await h.fire();
    await h.fire();
    await h.fire();
    assert.deepEqual(h.verdicts, [{ action: 'navigate', url: '/m/' }]);
    assert.equal(h.pending(), 0, 'no poll after the verdict');
    assert.equal(h.calls(), 3);
  });

  test('a failed fetch keeps polling', async () => {
    const h = harness([new Error('offline'), { status: 'open' }]);
    h.poller.start();
    await h.fire();
    assert.equal(h.pending(), 1);
    assert.deepEqual(h.verdicts, []);
  });

  test('does not schedule while hidden, and resumes on tick', async () => {
    const h = harness([{ status: 'expired', reason: 'Gone.' }], { visible: false });
    h.poller.start();
    assert.equal(h.pending(), 0);
    h.state.visible = true;
    await h.poller.tick();
    assert.deepEqual(h.verdicts, [{ action: 'ended', reason: 'Gone.' }]);
    assert.equal(h.pending(), 0);
  });

  test('pause clears the pending poll', () => {
    const h = harness([{ status: 'open' }]);
    h.poller.start();
    h.poller.pause();
    assert.equal(h.pending(), 0);
  });
});

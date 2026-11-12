// The service worker's route table (KAN-82) must leave the live JSON
// endpoints alone (KAN-168): a held /live/state/?wait=1 answered or cached
// by the worker would never wake on the other phone's point.
//
// sw.js runs in a vm (sw_harness.mjs). A fetch event the worker claims calls
// respondWith(); a pass-through does not.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { fetchEvent, loadWorker } from './sw_harness.mjs';

function claims(listeners, path, { method = 'GET', mode = 'cors' } = {}) {
  const event = fetchEvent(path, { method, mode });
  listeners.fetch(event);
  return event.responded;
}

describe('service worker route table', () => {
  const { listeners } = loadWorker();

  test('the long-poll passes through untouched', () => {
    assert.equal(claims(listeners, '/pingpong/matches/7/live/state/?since=3&wait=1'), false);
  });

  test('an instant state read passes through untouched', () => {
    assert.equal(claims(listeners, '/pingpong/matches/7/live/state/?since=3&wait=0'), false);
    assert.equal(claims(listeners, '/pingpong/matches/7/live/state/'), false);
  });

  test('ops and sync pass through untouched', () => {
    assert.equal(claims(listeners, '/pingpong/matches/7/live/sync/', { method: 'POST' }), false);
    assert.equal(claims(listeners, '/pingpong/matches/7/live/point/', { method: 'POST' }), false);
  });

  test('sanity: the scoreboard navigation IS claimed', () => {
    // Proves the harness can see a claim, so the pass-throughs above mean something.
    assert.equal(claims(listeners, '/pingpong/matches/7/live/', { mode: 'navigate' }), true);
  });
});

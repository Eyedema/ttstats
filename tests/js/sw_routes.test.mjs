// The service worker's route table (KAN-82) must leave the live JSON
// endpoints alone (KAN-168): a held /live/state/?wait=1 answered or cached
// by the worker would never wake on the other phone's point.
//
// sw.js is a Django template; its four template expressions are replaced
// with inert values and the file is run in a vm with a fake `self`. A fetch
// event the worker claims calls respondWith(); a pass-through does not.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, test } from 'node:test';
import vm from 'node:vm';

const ORIGIN = 'https://ttstats.test';
const SOURCE = readFileSync(
  new URL('../../ttstats/pingpong/templates/pingpong/sw.js', import.meta.url),
  'utf8'
)
  .replace('{% load static %}', '')
  .replace('{{ sw_build }}', 'test')
  .replace('{{ sw_precache_json|safe }}', '[]')
  .replace(/\{% static "([^"]+)" %\}/, '/static/$1');

function loadWorker() {
  const listeners = {};
  const self = {
    location: new URL(ORIGIN + '/sw.js'),
    addEventListener: (type, fn) => {
      listeners[type] = fn;
    },
    skipWaiting: () => Promise.resolve(),
    clients: { claim: () => Promise.resolve(), matchAll: () => Promise.resolve([]) },
    registration: { showNotification: () => Promise.resolve() },
  };
  const context = {
    self,
    URL,
    Promise,
    Response: class {},
    caches: { open: () => new Promise(() => {}), keys: () => Promise.resolve([]) },
    fetch: () => new Promise(() => {}),
    setTimeout: () => 0, // never fires: nothing here waits on a navigation
    console,
  };
  vm.runInNewContext(SOURCE, context);
  assert.equal(typeof listeners.fetch, 'function', 'sw.js registers a fetch listener');
  return listeners;
}

function claims(listeners, path, { method = 'GET', mode = 'cors' } = {}) {
  let responded = false;
  listeners.fetch({
    request: { url: ORIGIN + path, method, mode, headers: new Map() },
    respondWith: () => {
      responded = true;
    },
    waitUntil: () => {},
  });
  return responded;
}

describe('service worker route table', () => {
  const listeners = loadWorker();

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

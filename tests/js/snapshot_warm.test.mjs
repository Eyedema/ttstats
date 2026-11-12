// KAN-136: the page side of the background warm-up. When to ask the worker,
// and the URL list it sends. Pure; no DOM. The worker side is
// tests/js/sw_warm.test.mjs.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, test } from 'node:test';

const require = createRequire(import.meta.url);
const Warm = require(
  new URL('../../ttstats/pingpong/static/pingpong/js/snapshot_warm.js', import.meta.url).pathname
);

describe('shouldWarm', () => {
  const ok = { connectionState: 'online', saveData: false, freshness: 'network' };

  test('online, no Save-Data, a live page: yes', () => {
    assert.equal(Warm.shouldWarm(ok), true);
  });
  test('degraded or offline: no', () => {
    assert.equal(Warm.shouldWarm({ ...ok, connectionState: 'degraded' }), false);
    assert.equal(Warm.shouldWarm({ ...ok, connectionState: 'offline' }), false);
  });
  test('no connection.js (no state): no', () => {
    assert.equal(Warm.shouldWarm({ ...ok, connectionState: null }), false);
    assert.equal(Warm.shouldWarm({ ...ok, connectionState: undefined }), false);
  });
  test('Save-Data: no', () => {
    assert.equal(Warm.shouldWarm({ ...ok, saveData: true }), false);
  });
  test('a saved copy never warms', () => {
    assert.equal(Warm.shouldWarm({ ...ok, freshness: 'snapshot' }), false);
  });
  test('no environment: no', () => {
    assert.equal(Warm.shouldWarm(null), false);
  });
});

describe('warmUrls', () => {
  test('keeps path-only /pingpong/ URLs, in order', () => {
    assert.deepEqual(Warm.warmUrls(['/pingpong/', '/pingpong/leaderboard/']), [
      '/pingpong/',
      '/pingpong/leaderboard/',
    ]);
  });
  test('drops anything else', () => {
    assert.deepEqual(
      Warm.warmUrls([
        'https://evil.test/pingpong/',
        '//evil.test/pingpong/',
        '/admin/',
        '/pingpong/matches/3/live/',
        42,
        null,
        '/pingpong/players/1/',
      ]),
      ['/pingpong/players/1/']
    );
  });
  test('each once, at most six', () => {
    const many = ['/pingpong/', '/pingpong/'].concat(
      Array.from({ length: 9 }, (_, i) => `/pingpong/matches/${i}/`)
    );
    const urls = Warm.warmUrls(many);
    assert.equal(urls.length, Warm.WARM_MAX);
    assert.equal(Warm.WARM_MAX, 6);
    assert.deepEqual(urls.slice(0, 2), ['/pingpong/', '/pingpong/matches/0/']);
  });
  test('not a list: nothing', () => {
    assert.deepEqual(Warm.warmUrls({ urls: [] }), []);
    assert.deepEqual(Warm.warmUrls(undefined), []);
  });
});

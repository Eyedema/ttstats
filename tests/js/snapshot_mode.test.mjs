// KAN-146: read-only mode on a saved copy -- the pure parts of snapshot_mode.js.
//
// The patterns are the server's own (offline.offline_navigation_paths(),
// written to ttstats/pingpong/tests/fixtures/snapshot_paths.json;
// test_offline_snapshots.py fails when that file is stale).
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { describe, test } from 'node:test';

const require = createRequire(import.meta.url);
const Mode = require(new URL('../../ttstats/pingpong/static/pingpong/js/snapshot_mode.js', import.meta.url).pathname);
const sources = JSON.parse(readFileSync(
  new URL('../../ttstats/pingpong/tests/fixtures/snapshot_paths.json', import.meta.url), 'utf8'));
const patterns = Mode.compilePatterns(sources);
const HERE = 'https://tt.example/pingpong/leaderboard/?season=3';
const classify = (href, here = HERE) => Mode.classifyLink(href, here, patterns);

describe('compilePatterns', () => {
  test('every server pattern compiles as a JS regex', () => {
    assert.equal(patterns.length, sources.length);
  });

  test('a broken source is dropped, never thrown', () => {
    assert.equal(Mode.compilePatterns(['^/ok/$', '(', null]).length, 2);
    assert.deepEqual(Mode.compilePatterns(undefined), []);
  });
});

describe('classifyLink', () => {
  test('saved-copy pages pass', () => {
    for (const href of [
      '/pingpong/', '/pingpong/leaderboard/', '/pingpong/leaderboard/?season=2',
      '/pingpong/matches/', '/pingpong/matches/42/', '/pingpong/players/7/',
      '/pingpong/head-to-head/?player1=1&player2=2', '/pingpong/championships/',
      '/pingpong/championships/3/', '/pingpong/calendar/', '/pingpong/seasons/',
      '/pingpong/seasons/5/',
    ]) {
      assert.equal(classify(href), 'pass', href);
    }
  });

  test('the scoreboard passes: the worker keeps its own copy (KAN-82)', () => {
    assert.equal(classify('/pingpong/matches/42/live/'), 'pass');
  });

  test('forms, actions and everything else need a connection', () => {
    for (const href of [
      '/pingpong/matches/add/', '/pingpong/matches/42/edit/', '/pingpong/matches/42/confirm/',
      '/pingpong/matches/42/live/state/', '/pingpong/live/join/abc/', '/pingpong/players/',
      '/pingpong/settings/notifications/', '/accounts/logout/', '/admin/', '/pingpong',
      '/pingpong/matches/x/', '/pingpong/leaderboardx/', '/sw.js',
    ]) {
      assert.equal(classify(href), 'block', href);
    }
  });

  test('another origin needs a connection; other schemes are not ours', () => {
    assert.equal(classify('https://example.org/pingpong/'), 'block');
    assert.equal(classify('mailto:someone@example.org'), 'pass');
    assert.equal(classify('tel:+3912345'), 'pass');
  });

  test('a fragment of this very page makes no request', () => {
    assert.equal(classify('#standings', 'https://tt.example/pingpong/settings/'), 'pass');
    assert.equal(classify('/pingpong/settings/#x', 'https://tt.example/pingpong/settings/'), 'pass');
    // ...but the same path with another query is another page.
    assert.equal(classify('/pingpong/settings/?a=1#x', 'https://tt.example/pingpong/settings/'), 'block');
  });

  test('anchored: a prefix or a suffix of an allowed path is not allowed', () => {
    assert.equal(Mode.classifyLink('/pingpong/x/pingpong/', HERE, patterns), 'block');
    assert.equal(Mode.classifyLink('/pingpong/matches/1/2/', HERE, patterns), 'block');
  });

  test('no patterns at all: every same-origin link is blocked', () => {
    assert.equal(Mode.classifyLink('/pingpong/', HERE, []), 'block');
  });
});

describe('relativeAge', () => {
  const t0 = Date.UTC(2026, 10, 12, 14, 32);
  const min = 60000;
  const cases = [
    [0, 'just now'], [59 * 1000, 'just now'], [min, '1 min ago'], [12 * min, '12 min ago'],
    [59 * min, '59 min ago'], [60 * min, '1 h ago'], [23 * 60 * min + 59 * min, '23 h ago'],
    [24 * 60 * min, '1 day ago'], [8 * 24 * 60 * min, '8 days ago'],
  ];
  for (const [elapsed, want] of cases) {
    test(`${elapsed / 1000}s -> ${want}`, () => assert.equal(Mode.relativeAge(t0, t0 + elapsed), want));
  }

  test('a clock behind the copy reads "just now", never negative', () => {
    assert.equal(Mode.relativeAge(t0, t0 - 5 * min), 'just now');
  });
});

describe('userTriggered', () => {
  test('a tap, a change or a submit is the user', () => {
    for (const type of ['click', 'change', 'submit', 'keyup']) {
      assert.equal(Mode.userTriggered({ type }), true, type);
    }
  });

  test('load, reveal, intersect and polling (no event) are not', () => {
    for (const ev of [{ type: 'load' }, { type: 'revealed' }, { type: 'intersect' }, undefined, null, {}]) {
      assert.equal(Mode.userTriggered(ev), false, JSON.stringify(ev));
    }
  });
});

describe('back-online probe', () => {
  // KAN-109: /healthz is outside every worker route and never cached; the
  // manifest it replaced said nothing about the database.
  test('asks /healthz, at the root', () => {
    assert.equal(Mode.PROBE_URL, '/healthz');
  });
});

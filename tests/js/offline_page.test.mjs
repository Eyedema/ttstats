// KAN-137: the offline page's list of saved pages, built from the worker's
// {type: 'list-snapshots'} reply. Pure grouping only; no DOM.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, test } from 'node:test';

const require = createRequire(import.meta.url);
const OfflinePage = require(
  new URL('../../ttstats/pingpong/static/pingpong/js/offline_page.js', import.meta.url).pathname
);

const ORIGIN = 'https://ttstats.test';
const entry = (path, title = '', shown = '14:32') => ({
  url: ORIGIN + path,
  title,
  savedAt: '2026-11-04T13:32:00.000Z',
  shown,
});

describe('groupFor', () => {
  const cases = [
    ['/pingpong/', 'today'],
    ['/pingpong/leaderboard/', 'table'],
    ['/pingpong/seasons/', 'table'],
    ['/pingpong/seasons/4/', 'table'],
    ['/pingpong/matches/', 'matches'],
    ['/pingpong/matches/12/', 'matches'],
    ['/pingpong/players/3/', 'players'],
    ['/pingpong/head-to-head/', 'players'],
    ['/pingpong/championships/2/', 'other'],
    ['/pingpong/calendar/', 'other'],
  ];
  for (const [path, group] of cases) {
    test(`${path} -> ${group}`, () => assert.equal(OfflinePage.groupFor(path), group));
  }
});

describe('buildGroups', () => {
  test('scoreboards first, then Today / Table / Matches / Players / Other', () => {
    const groups = OfflinePage.buildGroups(
      {
        type: 'snapshots',
        snapshots: [
          entry('/pingpong/calendar/', 'Calendar - Table Tennis Tracker'),
          entry('/pingpong/players/3/', 'Ann - Table Tennis Tracker'),
          entry('/pingpong/matches/12/', 'Ann vs Bob - TTStats'),
          entry('/pingpong/leaderboard/?season=2', 'The Table - TTStats'),
          entry('/pingpong/', 'Today - TTStats'),
        ],
        scoreboards: [entry('/pingpong/matches/9/live/', 'Live - TTStats')],
      },
      ORIGIN
    );
    assert.deepEqual(
      groups.map((g) => g.key),
      ['live', 'today', 'table', 'matches', 'players', 'other']
    );
    assert.equal(groups[0].label, 'Live scoreboards you can keep scoring');
    assert.deepEqual(groups[2].items, [
      { path: '/pingpong/leaderboard/?season=2', pathname: '/pingpong/leaderboard/', title: 'The Table', shown: '14:32' },
    ]);
    assert.equal(groups[5].items[0].title, 'Calendar');
  });

  test('keeps the worker order (newest first) inside a group', () => {
    const groups = OfflinePage.buildGroups(
      { snapshots: [entry('/pingpong/matches/2/'), entry('/pingpong/matches/1/')] },
      ORIGIN
    );
    assert.deepEqual(groups[0].items.map((i) => i.path), ['/pingpong/matches/2/', '/pingpong/matches/1/']);
  });

  test('empty groups are left out; an empty reply is no groups', () => {
    assert.deepEqual(OfflinePage.buildGroups({ snapshots: [], scoreboards: [] }, ORIGIN), []);
    assert.deepEqual(OfflinePage.buildGroups(null, ORIGIN), []);
    assert.deepEqual(OfflinePage.buildGroups({ snapshots: 'nope' }, ORIGIN), []);
  });

  test('never a link out of the app', () => {
    const groups = OfflinePage.buildGroups(
      {
        snapshots: [
          { url: 'https://evil.test/pingpong/', title: 'x' },
          { url: ORIGIN + '/admin/', title: 'x' },
          { url: 'javascript:alert(1)', title: 'x' },
          { url: ORIGIN + '/pingpong/', title: '' },
        ],
      },
      ORIGIN
    );
    assert.deepEqual(groups.map((g) => g.items.map((i) => i.path)), [['/pingpong/']]);
  });

  test('a page without a title is listed by its path; each URL once', () => {
    const groups = OfflinePage.buildGroups(
      { snapshots: [entry('/pingpong/calendar/'), entry('/pingpong/calendar/', 'Again')] },
      ORIGIN
    );
    assert.deepEqual(groups[0].items.map((i) => i.title), ['/pingpong/calendar/']);
  });
});

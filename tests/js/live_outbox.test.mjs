// KAN-93: the durable outbox. In-memory storage shim; no DOM.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, test } from 'node:test';
import { memoryStorage } from './live_fakes.mjs';

const require = createRequire(import.meta.url);
const LiveOutbox = require(new URL('../../ttstats/pingpong/static/pingpong/js/live_outbox.js', import.meta.url).pathname);

function open(storage, extra = {}) {
  const reports = [];
  const outbox = LiveOutbox.load({
    storage,
    matchPk: 7,
    userId: 3,
    now: () => Date.UTC(2026, 10, 7, 12, 0, 0),
    report: (message, detail) => reports.push({ message, detail }),
    ...extra,
  });
  return { outbox, reports };
}

describe('LiveOutbox', () => {
  test('append assigns gapless seqs after the applied one and persists before returning', () => {
    const storage = memoryStorage();
    const { outbox } = open(storage);
    const a = outbox.append({ type: 'start', initial_server: 'team1' }, 4);
    const b = outbox.append({ type: 'point', side: 'team2' }, 4);
    assert.equal(a.seq, 5);
    assert.equal(b.seq, 6);
    assert.equal(b.client_ts, '2026-11-07T12:00:00.000Z');
    const stored = JSON.parse(storage.getItem(outbox.key));
    assert.deepEqual(stored.ops.map((op) => op.seq), [5, 6]);
  });

  test('the key is scoped by match AND user', () => {
    const storage = memoryStorage();
    const mine = LiveOutbox.load({ storage, matchPk: 7, userId: 3 });
    mine.append({ type: 'point', side: 'team1' }, 0);
    const someoneElse = LiveOutbox.load({ storage, matchPk: 7, userId: 4 });
    const otherMatch = LiveOutbox.load({ storage, matchPk: 8, userId: 3 });
    assert.equal(someoneElse.size(), 0);
    assert.equal(otherMatch.size(), 0);
    assert.notEqual(LiveOutbox.storageKey(7, 3), LiveOutbox.storageKey(7, 4));
  });

  test('survives a reload: a new load sees the same ops and keeps numbering', () => {
    const storage = memoryStorage();
    const first = open(storage).outbox;
    for (let i = 0; i < 12; i += 1) first.append({ type: 'point', side: 'team1' }, 0);
    const reloaded = open(storage).outbox;
    assert.equal(reloaded.size(), 12);
    assert.equal(reloaded.nextSeq(0), 13);
  });

  test('ackThrough drops acknowledged ops and a seq is never reused afterwards', () => {
    const storage = memoryStorage();
    const { outbox } = open(storage);
    outbox.append({ type: 'point', side: 'team1' }, 0);
    outbox.append({ type: 'point', side: 'team1' }, 0);
    outbox.append({ type: 'undo' }, 0);
    assert.equal(outbox.ackThrough(2), 2);
    assert.deepEqual(outbox.pending().map((op) => op.seq), [3]);
    outbox.ackThrough(3);
    // Even with the server answer lost, the next op is 4, never 1 again.
    assert.equal(outbox.nextSeq(0), 4);
    assert.equal(open(storage).outbox.nextSeq(0), 4);
  });

  test('nextSeq follows the server when it is ahead', () => {
    const { outbox } = open(memoryStorage());
    assert.equal(outbox.nextSeq(41), 42);
  });

  test('pending() returns copies', () => {
    const { outbox } = open(memoryStorage());
    outbox.append({ type: 'point', side: 'team1' }, 0);
    outbox.pending()[0].side = 'team2';
    assert.equal(outbox.pending()[0].side, 'team1');
  });

  test('clear() forgets ops, snapshot and numbering', () => {
    const storage = memoryStorage();
    const { outbox } = open(storage);
    outbox.append({ type: 'point', side: 'team1' }, 0);
    outbox.saveSnapshot({ applied_seq: 0, state: {} });
    outbox.clear();
    assert.equal(storage.getItem(outbox.key), null);
    assert.equal(outbox.nextSeq(9), 10);
  });

  test('a snapshot is kept only if it is not older than the stored one', () => {
    const storage = memoryStorage();
    const { outbox } = open(storage);
    outbox.saveSnapshot({ applied_seq: 9, state: { team1_points: 3 } });
    outbox.saveSnapshot({ applied_seq: 4, state: { team1_points: 1 } });
    assert.equal(open(storage).outbox.snapshot().applied_seq, 9);
  });

  for (const [why, raw] of [
    ['not JSON', '{nope'],
    ['wrong version', JSON.stringify({ v: 99, match: 7, user: 3, ops: [] })],
    ['another user', JSON.stringify({ v: 1, match: 7, user: 4, ops: [] })],
    ['a malformed op', JSON.stringify({ v: 1, match: 7, user: 3, ops: [{ seq: 1, type: 'point', side: 'team9' }] })],
    ['a duplicate seq', JSON.stringify({ v: 1, match: 7, user: 3, ops: [
      { seq: 1, type: 'undo' }, { seq: 1, type: 'undo' }] })],
    ['a bad snapshot', JSON.stringify({ v: 1, match: 7, user: 3, ops: [], snapshot: { applied_seq: 'x' } })],
  ]) {
    test(`corrupt storage (${why}) is discarded and reported, never thrown`, () => {
      const storage = memoryStorage({ [LiveOutbox.storageKey(7, 3)]: raw });
      const { outbox, reports } = open(storage);
      assert.equal(outbox.size(), 0);
      assert.equal(reports.length, 1);
      assert.match(reports[0].message, /corrupt/);
      assert.equal(storage.getItem(outbox.key), null);
      // And it works normally afterwards.
      assert.equal(outbox.append({ type: 'point', side: 'team1' }, 0).seq, 1);
    });
  }

  test('storage that refuses writes leaves a working in-memory outbox', () => {
    const storage = memoryStorage();
    storage.setItem = () => { throw new Error('QuotaExceededError'); };
    const { outbox, reports } = open(storage);
    const op = outbox.append({ type: 'point', side: 'team1' }, 0);
    assert.equal(op.seq, 1);
    assert.equal(outbox.size(), 1);
    assert.equal(outbox.isPersistent(), false);
    assert.equal(reports.length, 1);
  });

  test('storage that throws on read starts empty', () => {
    const storage = memoryStorage();
    storage.getItem = () => { throw new Error('SecurityError'); };
    const { outbox } = open(storage);
    assert.equal(outbox.size(), 0);
  });

  test('no storage at all still works', () => {
    const outbox = LiveOutbox.load({ storage: null, matchPk: 1, userId: 1 });
    assert.equal(outbox.append({ type: 'undo' }, 0).seq, 1);
    assert.equal(outbox.isPersistent(), false);
  });

  test('an invalid op is refused before it is stored', () => {
    const { outbox } = open(memoryStorage());
    assert.throws(() => outbox.append({ type: 'point', side: 'left' }, 0), TypeError);
    assert.equal(outbox.size(), 0);
  });

  test('requestPersistence is best effort', async () => {
    assert.equal(await LiveOutbox.requestPersistence(undefined), false);
    assert.equal(await LiveOutbox.requestPersistence({ storage: { persist: () => Promise.resolve(true) } }), true);
    assert.equal(await LiveOutbox.requestPersistence({ storage: { persist: () => Promise.reject(new Error('no')) } }), false);
    assert.equal(await LiveOutbox.requestPersistence({ storage: { persist() { throw new Error('no'); } } }), false);
  });
});

/*
 * live_outbox.js -- the scoreboard's durable outbox (KAN-93).
 *
 * Every scorekeeper action becomes an op with a client `seq` (per match, from
 * 1, gapless; see .claude/rules/live-scoring.md). The op is written here
 * BEFORE anything is sent, so a dead connection, a reload or iOS killing a
 * backgrounded tab never costs a tap. The server dedupes by seq, so sending
 * an op twice is always safe; losing one is not.
 *
 * Storage is localStorage, one key per (match, user): two accounts on one
 * phone never share an outbox. Why not IndexedDB: a match is a few hundred
 * ops of ~80 bytes, the write must be durable before the tap handler returns
 * (localStorage is synchronous; an IndexedDB transaction is not, and a tab
 * killed between the tap and the commit would lose the op), and it needs no
 * wrapper library. The cost -- iOS may evict script-writable storage after
 * seven days without a visit -- is irrelevant for a match played today.
 * Recorded in docs/decisions/kan-80-81-offline-client.md.
 *
 * Besides the ops, the record keeps the last server snapshot the client
 * acknowledged (`snapshot`: the canonical payload and its applied_seq). A
 * page served stale -- the service worker's network-first fallback while
 * offline -- carries an old bootstrap; the snapshot is newer and wins, so
 * "base + pending ops" is always rebuilt on the right base.
 *
 * Nothing here throws into a caller. Corrupt or foreign data is discarded and
 * reported; storage that refuses writes (Safari private mode, quota) leaves
 * the outbox working in memory, with `persistent` false.
 *
 * UMD: a classic script sets window.LiveOutbox; Node require()s it.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.LiveOutbox = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var VERSION = 1;
  var KEY_PREFIX = 'ttstats:live-outbox:v' + VERSION + ':';
  var OP_TYPES = ['start', 'point', 'undo', 'side_switch'];

  function storageKey(matchPk, userId) {
    return KEY_PREFIX + String(matchPk) + ':' + String(userId);
  }

  function isValidOp(op) {
    if (!op || typeof op !== 'object') return false;
    if (!Number.isInteger(op.seq) || op.seq < 1) return false;
    if (OP_TYPES.indexOf(op.type) === -1) return false;
    if (op.type === 'point' && op.side !== 'team1' && op.side !== 'team2') return false;
    if (op.type === 'start' && op.initial_server !== 'team1' && op.initial_server !== 'team2') {
      return false;
    }
    return true;
  }

  function isValidSnapshot(snapshot) {
    if (snapshot === null) return true;
    return Boolean(snapshot) && typeof snapshot === 'object' &&
      Number.isInteger(snapshot.applied_seq) && snapshot.applied_seq >= 0 &&
      Boolean(snapshot.payload) && typeof snapshot.payload === 'object';
  }

  // Parse and validate a stored record. Returns null for anything we would
  // not trust to replay onto a match.
  function parseRecord(raw, matchPk, userId) {
    var record = JSON.parse(raw);
    if (!record || typeof record !== 'object' || record.v !== VERSION) return null;
    if (String(record.match) !== String(matchPk) || String(record.user) !== String(userId)) {
      return null;
    }
    if (!Array.isArray(record.ops) || !record.ops.every(isValidOp)) return null;
    if (!isValidSnapshot(record.snapshot === undefined ? null : record.snapshot)) return null;
    var seen = {};
    for (var i = 0; i < record.ops.length; i += 1) {
      if (seen[record.ops[i].seq]) return null;
      seen[record.ops[i].seq] = true;
    }
    return record;
  }

  function maxSeq(ops) {
    return ops.reduce(function (m, op) { return Math.max(m, op.seq); }, 0);
  }

  function copyOp(op) {
    return Object.assign({}, op);
  }

  /**
   * Open the outbox for one match and one user.
   *
   * options.storage   a Storage-like object (getItem/setItem/removeItem);
   *                   defaults to window.localStorage when present.
   * options.matchPk / options.userId   the scope; both required.
   * options.report(message, extra)     where corruption and storage failures
   *                   are reported (the page wires it to its report hook).
   * options.now()     clock for client_ts; defaults to Date.now.
   */
  function load(options) {
    options = options || {};
    var matchPk = options.matchPk;
    var userId = options.userId;
    if (matchPk === undefined || matchPk === null || userId === undefined || userId === null) {
      throw new TypeError('LiveOutbox.load needs matchPk and userId');
    }
    var report = typeof options.report === 'function' ? options.report : function () {};
    var now = typeof options.now === 'function' ? options.now : function () { return Date.now(); };
    var storage = options.storage;
    if (storage === undefined) {
      try {
        storage = typeof localStorage !== 'undefined' ? localStorage : null;
      } catch (err) {
        storage = null; // Access itself can throw (blocked cookies on Safari).
      }
    }
    var key = storageKey(matchPk, userId);

    var ops = [];
    var snapshot = null;
    var games = {};
    // The highest seq this outbox ever handed out. Kept so a seq is never
    // reused after its op was acknowledged and removed.
    var lastSeq = 0;
    var persistent = Boolean(storage);

    if (storage) {
      var raw = null;
      try {
        raw = storage.getItem(key);
      } catch (err) {
        persistent = false;
        report('live outbox: storage unreadable', { match: matchPk, error: String(err) });
      }
      if (raw !== null && raw !== undefined) {
        var record = null;
        try {
          record = parseRecord(raw, matchPk, userId);
        } catch (err) {
          record = null;
        }
        if (record) {
          ops = record.ops.map(copyOp).sort(function (a, b) { return a.seq - b.seq; });
          snapshot = record.snapshot || null;
          games = record.games && typeof record.games === 'object' ? record.games : {};
          lastSeq = Math.max(Number.isInteger(record.lastSeq) ? record.lastSeq : 0, maxSeq(ops));
        } else {
          report('live outbox: discarded a corrupt record', { match: matchPk });
          try {
            storage.removeItem(key);
          } catch (err) {
            // Nothing more to do; we start empty either way.
          }
        }
      }
    }

    function save() {
      if (!storage) return false;
      var empty = !ops.length && !snapshot && !Object.keys(games).length;
      try {
        if (empty && lastSeq === 0) {
          storage.removeItem(key);
        } else {
          storage.setItem(key, JSON.stringify({
            v: VERSION,
            match: matchPk,
            user: userId,
            ops: ops,
            snapshot: snapshot,
            games: games,
            lastSeq: lastSeq,
            savedAt: new Date(now()).toISOString(),
          }));
        }
        persistent = true;
        return true;
      } catch (err) {
        if (persistent) {
          report('live outbox: storage refused a write', { match: matchPk, error: String(err) });
        }
        persistent = false;
        return false;
      }
    }

    var outbox = {
      key: key,

      // Ops not yet acknowledged, oldest first. Copies: callers cannot
      // mutate the outbox behind its back.
      pending: function () {
        return ops.map(copyOp);
      },

      size: function () {
        return ops.length;
      },

      isPersistent: function () {
        return persistent;
      },

      // The seq the next op gets: past everything the server applied and
      // everything this outbox ever issued.
      nextSeq: function (appliedSeq) {
        var applied = Number.isInteger(appliedSeq) ? appliedSeq : 0;
        return Math.max(applied, lastSeq, maxSeq(ops)) + 1;
      },

      // Assign the next seq and a client_ts, persist, return the stored op.
      // Persisting happens here, synchronously, before any network call.
      append: function (op, appliedSeq) {
        var stored = copyOp(op);
        stored.seq = outbox.nextSeq(appliedSeq);
        if (!stored.client_ts) stored.client_ts = new Date(now()).toISOString();
        if (!isValidOp(stored)) throw new TypeError('LiveOutbox.append: invalid op ' + JSON.stringify(op));
        ops.push(stored);
        lastSeq = stored.seq;
        save();
        return copyOp(stored);
      },

      // Drop every op the server has applied (or consumed by rejecting it).
      ackThrough: function (seq) {
        var before = ops.length;
        ops = ops.filter(function (op) { return op.seq > seq; });
        if (ops.length !== before) save();
        return before - ops.length;
      },

      // Drop one op that will never be sent (a client-side refusal).
      drop: function (seq) {
        var before = ops.length;
        ops = ops.filter(function (op) { return op.seq !== seq; });
        if (ops.length !== before) save();
      },

      // Shared scoring (KAN-153): the server's stream stops at `appliedSeq`
      // but this outbox's first pending op is further on, so the ops in
      // between are lost for good. Renumber what is left, in order, from
      // appliedSeq + 1 so it can still be sent. Never drops an op.
      renumberAfter: function (appliedSeq) {
        var next = (Number.isInteger(appliedSeq) ? appliedSeq : 0) + 1;
        ops = ops.map(function (op, i) { return Object.assign(copyOp(op), { seq: next + i }); });
        lastSeq = ops.length ? next + ops.length - 1 : Math.max(lastSeq, next - 1);
        save();
      },

      snapshot: function () {
        return snapshot ? JSON.parse(JSON.stringify(snapshot)) : null;
      },

      // Remember the latest canonical payload the server sent.
      saveSnapshot: function (payload) {
        if (!payload || typeof payload !== 'object') return;
        var applied = Number.isInteger(payload.applied_seq) ? payload.applied_seq : 0;
        if (snapshot && snapshot.applied_seq > applied) return;
        snapshot = { applied_seq: applied, payload: payload };
        save();
      },

      // Final scores of completed games, by game number, for the finish
      // screen: the server's state keeps only the last MAX_EVENTS events.
      games: function () {
        return Object.assign({}, games);
      },

      saveGames: function (next) {
        var json = JSON.stringify(next || {});
        if (json === JSON.stringify(games)) return;
        games = JSON.parse(json);
        save();
      },

      // Forget everything: the match completed, or this tab handed scoring
      // to another device.
      clear: function () {
        ops = [];
        snapshot = null;
        games = {};
        lastSeq = 0;
        if (!storage) return;
        try {
          storage.removeItem(key);
        } catch (err) {
          // A record we cannot remove is replaced by an empty outbox next time.
        }
      },
    };
    return outbox;
  }

  // Ask the browser not to evict our storage under pressure. Best effort:
  // unsupported, refused or throwing all mean "carry on".
  function requestPersistence(nav) {
    try {
      var storageManager = nav && nav.storage;
      if (!storageManager || typeof storageManager.persist !== 'function') return Promise.resolve(false);
      return Promise.resolve(storageManager.persist()).catch(function () { return false; });
    } catch (err) {
      return Promise.resolve(false);
    }
  }

  return Object.freeze({
    VERSION: VERSION,
    storageKey: storageKey,
    load: load,
    isValidOp: isValidOp,
    requestPersistence: requestPersistence,
  });
});

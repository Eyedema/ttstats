/*
 * live_session.js -- the scoreboard's state model and flush loop (KAN-94).
 *
 *   local state = server state (+ its applied_seq) + the outbox's pending ops,
 *                 replayed with live_rules.js
 *
 * The server stays the authority. A tap is applied locally (so the numerals,
 * the server indicator, game roll-over and the side-switch prompt all work
 * with no connection), appended to the outbox (persisted first), and a flush
 * is scheduled. The flush loop sends every pending op to live_sync in one
 * request and reconciles with the answer.
 *
 * Flush loop rules:
 *   - single flight: one request at a time, so responses can never arrive out
 *     of order -- there is nothing to reorder;
 *   - triggers: a tap (debounced, so a rally of taps coalesces), `online`,
 *     the page becoming visible, and a backoff timer while anything is
 *     pending (1/2/5/10/30 s, with jitter);
 *   - every request has an AbortController timeout: on slow 4G a hung fetch
 *     must not hold the queue forever;
 *   - reconcile: drop ops <= applied_seq; the server's state + the remaining
 *     ops is the new local state. If that differs from what the client had
 *     predicted for the same seq, the server wins and the drift is reported.
 *
 * Outcomes the page renders (`onEvent`):
 *   completed   the server acknowledged the end of the match (redirect_url)
 *   rejected    a rule rejection: the op is dropped, the score re-derived
 *   divergence  server state != predicted state: server wins
 *   conflict    another device is scoring this match: read-only follow mode
 *   auth        403 or a redirect to the login page: the outbox is kept and
 *               the loop stops until the user signs in again
 *
 * No DOM. Everything with a side effect (fetch, timers, clock, randomness,
 * connectivity) is injected, so tests/js/live_session.test.mjs drives it with
 * a fake transport. UMD: window.LiveSession, or require() in Node.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.LiveSession = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var DEFAULTS = {
    debounceMs: 150,
    timeoutMs: 8000,
    backoffMs: [1000, 2000, 5000, 10000, 30000],
    jitter: 0.2,
    maxBatch: 500,
  };

  // The fields that decide what the scoreboard shows. Timestamps differ
  // between the phone and the server by construction and are not compared.
  var COMPARED = [
    'started', 'initial_server', 'team1_points', 'team2_points',
    'team1_games', 'team2_games', 'current_game_number', 'side_switched',
  ];

  function comparable(state) {
    state = state || {};
    var out = {};
    COMPARED.forEach(function (key) {
      out[key] = state[key] === undefined ? null : state[key];
    });
    out.events = (state.events || []).map(function (e) {
      return [e.side, e.game, Boolean(e.game_end)];
    });
    return JSON.stringify(out);
  }

  function wireOp(op) {
    var out = { seq: op.seq, type: op.type };
    if (op.type === 'point') out.side = op.side;
    if (op.type === 'start') out.initial_server = op.initial_server;
    if (op.client_ts) out.client_ts = op.client_ts;
    return out;
  }

  /**
   * A fetch-backed transport. Answers are normalised to
   *   { status, body, loginRedirect }
   * where body is the parsed JSON (or null) and loginRedirect is true when
   * the request was bounced to a non-JSON page (the login form, after the
   * session expired). Network failures and aborts reject.
   */
  function fetchTransport(options) {
    var doFetch = options.fetch;
    var csrfToken = typeof options.csrfToken === 'function'
      ? options.csrfToken
      : function () { return options.csrfToken || ''; };

    function normalise(resp) {
      var type = (resp.headers && resp.headers.get && resp.headers.get('content-type')) || '';
      var isJson = type.indexOf('application/json') !== -1;
      var bounced = Boolean(resp.redirected) || (resp.ok && !isJson);
      if (!isJson) {
        return Promise.resolve({ status: resp.status, body: null, loginRedirect: bounced });
      }
      return resp.json().then(
        function (body) { return { status: resp.status, body: body, loginRedirect: bounced }; },
        function () { return { status: resp.status, body: null, loginRedirect: bounced }; }
      );
    }

    return {
      sync: function (ops, opts) {
        return doFetch(options.syncUrl, {
          method: 'POST',
          credentials: 'same-origin',
          cache: 'no-store',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json',
            'X-CSRFToken': csrfToken(),
          },
          body: JSON.stringify({ ops: ops }),
          signal: opts && opts.signal,
        }).then(normalise);
      },
      state: function (opts) {
        return doFetch(options.stateUrl, {
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
          signal: opts && opts.signal,
        }).then(normalise);
      },
    };
  }

  function create(options) {
    var rules = options.rules;
    var outbox = options.outbox;
    var transport = options.transport;
    var bestOf = options.bestOf;
    var timers = options.timers || {
      setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
      clearTimeout: function (id) { clearTimeout(id); },
    };
    var random = options.random || Math.random;
    var now = options.now || function () { return Date.now(); };
    var isOnline = options.isOnline || function () { return true; };
    var report = options.report || function () {};
    var onChange = options.onChange || function () {};
    var onEvent = options.onEvent || function () {};
    var makeController = options.makeController || function () {
      return typeof AbortController === 'function' ? new AbortController() : null;
    };
    var cfg = {};
    Object.keys(DEFAULTS).forEach(function (k) {
      cfg[k] = options[k] !== undefined ? options[k] : DEFAULTS[k];
    });

    // --- the server base ------------------------------------------------
    // The newer of the page's bootstrap and the outbox's stored snapshot:
    // a page served stale from the service worker carries an old bootstrap.
    var base = pickBase(options.bootstrap, outbox.snapshot());
    outbox.ackThrough(base.applied);

    var follow = false;
    var authRequired = false;
    var completedUrl = null;
    var lastFlushFailed = false;
    var failures = 0;
    var gapStrikes = 0;
    var lastSyncedAt = null;
    var inFlight = null;
    var again = false;
    var timer = null;
    var timerAt = null;
    var stopped = false;
    var local = derive();

    function pickBase(bootstrap, snapshot) {
      var fromPage = { payload: bootstrap || {}, applied: (bootstrap && bootstrap.applied_seq) || 0 };
      if (snapshot && snapshot.applied_seq > fromPage.applied) {
        return { payload: snapshot.payload, applied: snapshot.applied_seq };
      }
      return fromPage;
    }

    function baseState() {
      var state = base.payload && base.payload.state;
      if (!state || typeof state !== 'object' || !('started' in state)) {
        return rules.initialState(bestOf);
      }
      return state;
    }

    function applyOp(state, op) {
      switch (op.type) {
        case 'start':
          if (state.started) throw new Error('Match already started');
          return [rules.setInitialServer(state, op.initial_server), null];
        case 'point':
          return rules.applyPoint(state, op.side, { now: op.client_ts });
        case 'undo':
          return rules.undoLastPoint(state);
        case 'side_switch':
          return [rules.confirmSideSwitch(state), null];
        default:
          throw new Error('unknown op ' + op.type);
      }
    }

    // Replay ops onto a state. An op the mirror refuses is skipped: the
    // server will refuse it too, consume its seq, and say so in `rejected`.
    function replay(state, ops) {
      var games = {};
      (state.events || []).forEach(function (e) {
        if (e.game_end) games[e.game] = [e.final_t1, e.final_t2];
      });
      ops.forEach(function (op) {
        try {
          var result = applyOp(state, op);
          state = result[0];
          var done = op.type === 'point' ? result[1] : null;
          if (done) games[done.game_number] = [done.team1_score, done.team2_score];
        } catch (err) {
          // Refused locally; see above.
        }
      });
      return { state: state, games: games };
    }

    function derive() {
      var replayed = replay(baseState(), outbox.pending());
      var state = replayed.state;
      // Games before the current one: from the events, then from what this
      // outbox remembered (the events list is capped).
      var known = outbox.games();
      var games = {};
      Object.keys(known).concat(Object.keys(replayed.games)).forEach(function (n) {
        if (Number(n) < state.current_game_number) {
          games[n] = replayed.games[n] || known[n];
        }
      });
      outbox.saveGames(games);
      return { state: state, games: games };
    }

    function view() {
      var state = local.state;
      var pending = outbox.pending();
      var phase;
      if (completedUrl) phase = 'completed';
      else if (follow) phase = 'follow';
      else if (authRequired) phase = 'auth';
      else if (!pending.length) phase = 'synced';
      else if (!isOnline() || lastFlushFailed) phase = 'offline';
      else phase = 'pending';
      var gameList = Object.keys(local.games).map(Number).sort(function (a, b) { return a - b; })
        .map(function (n) { return { game_number: n, team1: local.games[n][0], team2: local.games[n][1] }; });
      return {
        state: state,
        currentServer: rules.currentServer(state),
        shouldPromptSideSwitch: Boolean(state.started) && rules.shouldPromptSideSwitch(state),
        complete: Boolean(state.started) && rules.isMatchComplete(state),
        winner: rules.matchWinner(state),
        games: gameList,
        pending: pending.length,
        pendingPoints: pending.filter(function (op) { return op.type === 'point'; }).length,
        appliedSeq: base.applied,
        phase: phase,
        inFlight: Boolean(inFlight),
        lastSyncedAt: lastSyncedAt,
        persistent: outbox.isPersistent(),
        redirectUrl: completedUrl,
      };
    }

    function changed() {
      local = derive();
      onChange(view());
    }

    // --- scheduling -----------------------------------------------------

    function schedule(ms) {
      if (stopped || completedUrl) return;
      var at = now() + ms;
      if (timer !== null && timerAt <= at) return; // an earlier run is already due
      if (timer !== null) timers.clearTimeout(timer);
      timerAt = at;
      timer = timers.setTimeout(function () {
        timer = null;
        timerAt = null;
        flush();
      }, ms);
    }

    function backoffDelay() {
      var steps = cfg.backoffMs;
      var baseDelay = steps[Math.min(failures - 1, steps.length - 1)];
      var spread = 1 - cfg.jitter + 2 * cfg.jitter * random();
      return Math.round(baseDelay * spread);
    }

    function fail(reason, extra) {
      failures += 1;
      lastFlushFailed = true;
      if (extra) report('live sync: ' + reason, extra);
      if (outbox.size()) schedule(backoffDelay());
    }

    // --- outcomes ---------------------------------------------------------

    function complete(url) {
      completedUrl = url;
      outbox.clear();
      if (timer !== null) timers.clearTimeout(timer);
      timer = null;
      onChange(view());
      onEvent('completed', { redirectUrl: url });
    }

    function enterFollow(payload, why) {
      follow = true;
      outbox.clear();
      if (payload) {
        base = { payload: payload, applied: payload.applied_seq || 0 };
      }
      report('live sync: another device is scoring this match', { reason: why, applied_seq: base.applied });
      changed();
      onEvent('conflict', { reason: why });
    }

    function reconcile(body, sent) {
      if (body.redirect_url && body.is_live === false) {
        complete(body.redirect_url);
        return;
      }
      var applied = Number.isInteger(body.applied_seq) ? body.applied_seq : 0;
      var issued = outbox.nextSeq(0) - 1;
      if (applied > issued) {
        // The server applied seqs this phone never issued: someone else is
        // scoring, and our ops at those seqs were taken as duplicates.
        enterFollow(body, 'applied_seq ahead of this device');
        return;
      }
      if (applied < base.applied) {
        report('live sync: server applied_seq went backwards', { was: base.applied, now: applied });
      }

      var pending = outbox.pending();
      var predicted = replay(baseState(), pending.filter(function (op) {
        return op.seq <= applied;
      })).state;
      var rejected = body.rejected || null;

      base = { payload: body, applied: applied };
      outbox.saveSnapshot(body);
      outbox.ackThrough(applied);
      failures = 0;
      lastFlushFailed = false;
      gapStrikes = 0;
      lastSyncedAt = now();

      if (rejected && rejected.error === 'gap') {
        // Something before the hole was applied; send the rest at once.
        schedule(0);
      } else if (rejected) {
        report('live sync: op rejected', { seq: rejected.seq, error: rejected.error, applied_seq: applied });
        onEvent('rejected', { seq: rejected.seq, error: rejected.error });
      } else if (comparable(predicted) !== comparable(body.state)) {
        report('live sync: local state diverged from the server', {
          applied_seq: applied, local: comparable(predicted), server: comparable(body.state),
        });
        onEvent('divergence', { applied_seq: applied });
      }
      changed();
      if (outbox.size()) schedule(cfg.debounceMs);
    }

    function handleGap(body) {
      var applied = (body && Number.isInteger(body.applied_seq)) ? body.applied_seq : base.applied;
      gapStrikes += 1;
      if (gapStrikes >= 2) {
        enterFollow(null, 'gap that did not resolve (server at ' + applied + ')');
        refresh({ force: true });
        return;
      }
      // Our ops start past the server's next seq, so ops it needs are gone
      // from this outbox. One retry in case it was a race; then give up.
      report('live sync: gap', { applied_seq: applied, first_pending: (outbox.pending()[0] || {}).seq });
      failures += 1;
      schedule(backoffDelay());
    }

    // --- the flush --------------------------------------------------------

    function flush() {
      if (stopped || completedUrl || follow) return Promise.resolve();
      if (inFlight) {
        again = true;
        return inFlight;
      }
      if (authRequired) return Promise.resolve();
      var sent = outbox.pending().slice(0, cfg.maxBatch);
      if (!sent.length) return Promise.resolve();
      if (timer !== null) {
        timers.clearTimeout(timer);
        timer = null;
        timerAt = null;
      }

      var controller = makeController();
      var timedOut = false;
      var deadline = null;
      var request;
      try {
        request = Promise.resolve(transport.sync(sent.map(wireOp), {
          signal: controller ? controller.signal : undefined,
        }));
      } catch (err) {
        request = Promise.reject(err);
      }
      // The deadline both aborts the fetch and settles the flush, so even a
      // transport that ignores the signal cannot hold the queue. A late
      // answer is ignored; the next flush re-sends and the server dedupes.
      var settled = new Promise(function (resolve, reject) {
        deadline = timers.setTimeout(function () {
          timedOut = true;
          if (controller) controller.abort();
          reject(new Error('timeout'));
        }, cfg.timeoutMs);
        request.then(resolve, reject);
      });

      onChange(Object.assign(view(), { inFlight: true }));
      inFlight = settled.then(function (resp) {
        timers.clearTimeout(deadline);
        inFlight = null;
        if (resp.loginRedirect || resp.status === 401 || resp.status === 403) {
          authRequired = true;
          lastFlushFailed = true;
          onChange(view());
          onEvent('auth', { status: resp.status });
          return;
        }
        if (resp.status === 409 && resp.body && resp.body.error === 'gap') {
          handleGap(resp.body);
          onChange(view());
          return;
        }
        if (resp.status >= 200 && resp.status < 300 && resp.body) {
          reconcile(resp.body, sent);
        } else if (resp.status >= 500) {
          fail('server error', null);
          onChange(view());
        } else {
          // 400/404/413: a client bug or a vanished match. Keep the ops,
          // report, and keep trying slowly.
          fail('unexpected answer', { status: resp.status, error: resp.body && resp.body.error });
          onChange(view());
        }
      }, function (err) {
        timers.clearTimeout(deadline);
        inFlight = null;
        fail(timedOut ? 'timeout' : 'network', null);
        onChange(view());
      }).then(function () {
        if (again && !completedUrl) {
          again = false;
          schedule(cfg.debounceMs);
        }
      });
      return inFlight;
    }

    // Re-read the canonical state (LiveStateView). Only adopted when the
    // outbox is empty, or in follow mode, or when forced.
    function refresh(opts) {
      if (stopped || completedUrl || !transport.state) return Promise.resolve();
      var force = opts && opts.force;
      return Promise.resolve(transport.state({})).then(function (resp) {
        if (!resp || !resp.body || resp.status !== 200) return;
        var body = resp.body;
        if (body.redirect_url && body.is_live === false) {
          if (!outbox.size() || follow || force) complete(body.redirect_url);
          return;
        }
        if (outbox.size() && !follow && !force) return;
        var applied = body.applied_seq || 0;
        if (applied < base.applied && !follow && !force) return;
        base = { payload: body, applied: applied };
        outbox.saveSnapshot(body);
        changed();
      }, function () {
        // Offline: keep showing what we have.
      });
    }

    // --- the public surface ----------------------------------------------

    var session = {
      view: view,

      // Apply an op locally, persist it, schedule a flush. Returns
      // {ok, op, completedGame} or {ok:false, error}.
      dispatch: function (op) {
        if (completedUrl) return { ok: false, error: 'Match is already complete' };
        if (follow) return { ok: false, error: 'Read-only: another device is scoring' };
        var result;
        try {
          result = applyOp(local.state, Object.assign({}, op, { client_ts: new Date(now()).toISOString() }));
        } catch (err) {
          return { ok: false, error: err.message };
        }
        var stored = outbox.append(op, base.applied);
        changed();
        schedule(cfg.debounceMs);
        return { ok: true, op: stored, completedGame: op.type === 'point' ? result[1] : null };
      },

      flush: flush,

      // Run a flush now: `online`, visible again, or the Retry button.
      nudge: function () {
        authRequired = false;
        failures = 0;
        if (outbox.size()) {
          if (timer !== null) timers.clearTimeout(timer);
          timer = null;
          return flush();
        }
        return refresh();
      },

      refresh: refresh,

      // Leave follow mode and score on this device from the server's state.
      takeOver: function () {
        follow = false;
        outbox.clear();
        gapStrikes = 0;
        return refresh({ force: true }).then(function () { changed(); });
      },

      stop: function () {
        stopped = true;
        if (timer !== null) timers.clearTimeout(timer);
        timer = null;
      },
    };
    onChange(view());
    if (outbox.size()) schedule(0);
    return session;
  }

  return Object.freeze({
    create: create,
    fetchTransport: fetchTransport,
    comparable: comparable,
    DEFAULTS: DEFAULTS,
  });
});

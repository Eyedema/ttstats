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
 * Shared scoring (KAN-153, `options.shared`): two phones, one stream each.
 * With `shared` false none of this runs and the client is the KAN-80/81 one.
 *   - Seqs and applied_seq are per stream (mine); `rev` is match-wide. A base
 *     is "newer" by (rev, applied_seq), so a long-poll answer and a flush
 *     answer arriving out of order never move the score backwards.
 *   - Rebase, not follow: the other phone's ops are part of the server state
 *     and my pending ops replay on top of them, silently. "Server wins"
 *     (divergence) is only judged when no other stream moved since the old
 *     base; otherwise the interleaving is unknown and the server is taken.
 *   - Follow mode only for the same user on another device (my stream's
 *     applied_seq past anything this outbox issued). A gap that does not
 *     resolve renumbers my pending ops after the server's applied_seq.
 *   - A long-poll loop (`transport.poll`, its own fetch and AbortController,
 *     independent of the flush): `?since=<rev>&wait=1` re-issued at once on
 *     200/204, ~1 s after a `busy` 204, backoff on errors; `wait=0` on load,
 *     visible again and `online`; nothing while hidden or after the match.
 *   - Undo carries `expects`: the sequence the trailing point will have,
 *     predicted from the base's last_point and my pending ops at the tap
 *     (see predictTrailing). A pending undo whose expects no longer matches
 *     the replay is skipped locally, exactly as the server will refuse it.
 *   - Pending points the mirror refuses on the new base (the other phone
 *     finished the match first) stay queued and are counted as `stuck`;
 *     the server decides when they arrive.
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
    // Shared scoring only. The long-poll's abort comes from the bootstrap
    // (`longpollAbortMs`, the server's wait + slack); these are client pacing.
    busyMs: 1000, // after a `X-Longpoll: busy` 204: another poll holds the slot
    minHoldMs: 1000, // a held poll answering 204 faster than this is not held
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
    if (op.type === 'undo' && Number.isInteger(op.expects)) out.expects = op.expects;
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
      // A 204 (long-poll: nothing changed) has no body by design; only a
      // redirect makes it a login bounce.
      if (resp.status === 204) {
        return Promise.resolve({ status: 204, body: null, loginRedirect: Boolean(resp.redirected) });
      }
      var type =(resp.headers && resp.headers.get && resp.headers.get('content-type')) || '';
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

    // A page the service worker served from its cache can carry a token
    // from an older CSRF secret. /live/state/ answers with the current one
    // (`csrf_token`); once learned it replaces the page's.
    var freshToken = null;
    function token() { return freshToken || csrfToken(); }

    function keepToken(resp) {
      if (resp.body && typeof resp.body.csrf_token === 'string' && resp.body.csrf_token) {
        freshToken = resp.body.csrf_token;
      }
      return resp;
    }

    function state(opts) {
      return doFetch(options.stateUrl, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        signal: opts && opts.signal,
      }).then(normalise).then(keepToken);
    }

    // The shared-scoring long-poll (KAN-168): `?since=<rev>[&wait=1]`.
    // Answers {status, body, loginRedirect, busy}; `busy` is the server's
    // `X-Longpoll: busy` (another held poll of this user has the slot).
    function poll(opts) {
      var sep = options.stateUrl.indexOf('?') === -1 ? '?' : '&';
      var url = options.stateUrl + sep + 'since=' + encodeURIComponent(String(opts.since)) +
        (opts.wait ? '&wait=1' : '');
      return doFetch(url, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        signal: opts.signal,
      }).then(function (resp) {
        var busy = Boolean(resp.headers && resp.headers.get && resp.headers.get('x-longpoll') === 'busy');
        return normalise(resp).then(function (out) {
          out.busy = busy;
          return keepToken(out);
        });
      });
    }

    function postSync(ops, opts) {
      return doFetch(options.syncUrl, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'X-CSRFToken': token(),
        },
        body: JSON.stringify({ ops: ops }),
        signal: opts && opts.signal,
      }).then(normalise);
    }

    return {
      // A 403 is retried once with the token /live/state/ hands out; only a
      // refusal with the current token is "sign in again". The retry is safe:
      // ops carry seqs, so nothing is applied twice.
      sync: function (ops, opts) {
        return postSync(ops, opts).then(function (resp) {
          if (resp.status !== 403 || resp.loginRedirect) return resp;
          var before = token();
          return state(opts).then(function (fresh) {
            if (fresh.status !== 200 || fresh.loginRedirect || token() === before) return resp;
            return postSync(ops, opts);
          }, function () { return resp; });
        });
      },
      state: state,
      poll: poll,
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
    // Shared scoring (KAN-153). Everything keyed on it is inert when false.
    var shared = Boolean(options.shared);
    var isVisible = options.isVisible || function () { return true; };
    // From the bootstrap (scoreboard-config.longpoll_abort_ms); never typed here.
    var longpollAbortMs = options.longpollAbortMs || null;

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
    // The long-poll (shared only): one request at a time, its own controller.
    var poll = { inFlight: false, controller: null, deadline: null, timer: null, failures: 0, token: 0 };
    var local = derive();

    function revOf(payload) {
      return payload && Number.isInteger(payload.rev) ? payload.rev : 0;
    }

    // Shared: is (rev, applied) of `payload` strictly past the base's? Both
    // only ever grow on the server, so this orders any two answers in time.
    function isNewer(payload, applied) {
      var rev = revOf(payload);
      var baseRev = revOf(base.payload);
      return rev > baseRev || (rev === baseRev && applied > base.applied);
    }

    function pickBase(bootstrap, snapshot) {
      var fromPage = { payload: bootstrap || {}, applied: (bootstrap && bootstrap.applied_seq) || 0 };
      if (!snapshot) return fromPage;
      if (shared) {
        var snapRev = revOf(snapshot.payload);
        var pageRev = revOf(fromPage.payload);
        if (snapRev > pageRev || (snapRev === pageRev && snapshot.applied_seq > fromPage.applied)) {
          return { payload: snapshot.payload, applied: snapshot.applied_seq };
        }
        return fromPage;
      }
      if (snapshot.applied_seq > fromPage.applied) {
        return { payload: snapshot.payload, applied: snapshot.applied_seq };
      }
      return fromPage;
    }

    // The sum of every other stream's applied_seq, from `scorers`; null when
    // the payload carries no presence. A change means another phone acted.
    function othersApplied(payload) {
      if (!payload || !Array.isArray(payload.scorers)) return null;
      var total = 0;
      payload.scorers.forEach(function (s) { total += Number(s.applied_seq) || 0; });
      return total - ((payload.applied_seq) || 0);
    }

    function lastPointSeq(payload) {
      var last = payload && payload.last_point;
      return last && Number.isInteger(last.sequence) ? last.sequence : 0;
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
    //
    // Shared only: `trailing` follows the sequence the trailing PointEvent
    // will have (the base's last_point, +1 per point, -1 per undo), an undo
    // whose `expects` does not match it is skipped (the server answers
    // "stale undo"), and refused points are counted as `stuck`.
    function replay(state, ops, trailingStart) {
      var games = {};
      var trailing = trailingStart || 0;
      var stuck = 0;
      (state.events || []).forEach(function (e) {
        if (e.game_end) games[e.game] = [e.final_t1, e.final_t2];
      });
      ops.forEach(function (op) {
        if (shared && op.type === 'undo' && op.expects !== trailing) return;
        try {
          var hadEvents = Boolean((state.events || []).length);
          var result = applyOp(state, op);
          state = result[0];
          var done = op.type === 'point' ? result[1] : null;
          if (done) games[done.game_number] = [done.team1_score, done.team2_score];
          if (op.type === 'point') trailing += 1;
          else if (op.type === 'undo' && hadEvents) trailing -= 1;
        } catch (err) {
          // Refused locally; see above.
          if (op.type === 'point') stuck += 1;
        }
      });
      return { state: state, games: games, trailing: trailing, stuck: stuck };
    }

    function derive() {
      var replayed = replay(baseState(), outbox.pending(), lastPointSeq(base.payload));
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
      return { state: state, games: games, trailing: replayed.trailing, stuck: shared ? replayed.stuck : 0 };
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
        // Shared scoring (KAN-153). `stuck`: pending points the mirror
        // refuses on the current base ("n points waiting to sync").
        // `trailingSeq`: the sequence an undo tapped now would send as
        // `expects`. `scorers`: presence for every phone (KAN-154's strip).
        shared: shared,
        stuck: local.stuck,
        trailingSeq: local.trailing,
        rev: revOf(base.payload),
        scorers: (base.payload && base.payload.scorers) || [],
        me: (base.payload && base.payload.me) || null,
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

    function backoffDelay(n) {
      if (n === undefined) n = failures;
      var steps = cfg.backoffMs;
      var baseDelay = steps[Math.min(n - 1, steps.length - 1)];
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
      stopPoll();
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
      var rejected = body.rejected || null;
      if (shared && !isNewer(body, applied)) {
        // A long-poll answer computed after this one already arrived and
        // was adopted (it carries my applied_seq too). Nothing to rebase;
        // only the rejection, if any, is news.
        failures = 0;
        lastFlushFailed = false;
        gapStrikes = 0;
        lastSyncedAt = now();
        if (rejected && rejected.error === 'gap') schedule(0);
        else if (rejected) announceRejection(rejected, applied);
        changed();
        if (outbox.size()) schedule(cfg.debounceMs);
        return;
      }
      if (applied < base.applied) {
        report('live sync: server applied_seq went backwards', { was: base.applied, now: applied });
      }

      var pending = outbox.pending();
      var predicted = replay(baseState(), pending.filter(function (op) {
        return op.seq <= applied;
      }), lastPointSeq(base.payload)).state;
      // Shared: when another phone acted since the old base, the server's
      // state interleaves its ops with mine in an order we cannot predict,
      // so a difference is expected, never "server wins".
      var othersMoved = shared && othersApplied(body) !== othersApplied(base.payload);

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
        announceRejection(rejected, applied);
      } else if (!othersMoved && comparable(predicted) !== comparable(body.state)) {
        report('live sync: local state diverged from the server', {
          applied_seq: applied, local: comparable(predicted), server: comparable(body.state),
        });
        onEvent('divergence', { applied_seq: applied });
      }
      changed();
      if (outbox.size()) schedule(cfg.debounceMs);
    }

    function announceRejection(rejected, applied) {
      if (shared && rejected.error === 'Match already started') {
        // The other phone picked the first server first: its choice is now
        // the base. Not an error anyone needs to read (KAN-153 §4).
        return;
      }
      if (shared && rejected.error === 'stale undo') {
        // Another point landed before the undo did: nothing was deleted.
        // The scorer sees the new trailing point and can undo again.
        onEvent('stale-undo', { seq: rejected.seq });
        return;
      }
      report('live sync: op rejected', { seq: rejected.seq, error: rejected.error, applied_seq: applied });
      onEvent('rejected', { seq: rejected.seq, error: rejected.error });
    }

    function handleGap(body) {
      var applied = (body && Number.isInteger(body.applied_seq)) ? body.applied_seq : base.applied;
      gapStrikes += 1;
      if (shared && gapStrikes >= 2 && typeof outbox.renumberAfter === 'function') {
        // My stream only: ops this outbox issued before its first pending
        // one are gone (storage lost them). Another scorer cannot cause a
        // gap in my stream, so this is not "someone else is scoring":
        // renumber what is left right after the server's seq and send it.
        report('live sync: gap, pending ops renumbered', {
          applied_seq: applied, first_pending: (outbox.pending()[0] || {}).seq,
        });
        outbox.renumberAfter(applied);
        gapStrikes = 0;
        schedule(0);
        return;
      }
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
        if (shared && !follow && !force) {
          adoptRemote(body);
          return;
        }
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

    // --- shared scoring: rebase on a payload I did not ask for (KAN-153) ---

    // A long-poll (or state) answer: the other phone's ops, maybe some of
    // mine. Adopted when newer than the base, whatever is pending: my
    // pending ops replay on top of it. Never "server wins" -- this is not an
    // answer to my ops.
    function adoptRemote(body) {
      if (!body || completedUrl) return;
      if (body.redirect_url && body.is_live === false) {
        // The match is over. With nothing of mine queued, follow the
        // server. Otherwise send what is queued: the server decides (it
        // answers "match complete" and the redirect), nothing is dropped here.
        if (!outbox.size() || follow) complete(body.redirect_url);
        else {
          stopPoll(); // nothing more to learn; the flush's answer completes
          schedule(0);
        }
        return;
      }
      var applied = Number.isInteger(body.applied_seq) ? body.applied_seq : 0;
      if (follow) {
        base = { payload: body, applied: applied };
        changed();
        return;
      }
      if (!isNewer(body, applied)) return;
      var issued = outbox.nextSeq(0) - 1;
      if (applied > Math.max(issued, base.applied)) {
        // My own stream moved past this outbox and past what this page
        // already knew: the same user is scoring on another device.
        // Another scorer's ops move `rev`, never my stream.
        enterFollow(body, 'applied_seq ahead of this device');
        return;
      }
      base = { payload: body, applied: applied };
      outbox.saveSnapshot(body);
      outbox.ackThrough(applied);
      changed();
    }

    // --- shared scoring: the long-poll loop (KAN-153, KAN-168) ------------
    //
    // Separate from the flush: its own request, controller and timers, so a
    // held poll never delays sending my ops. At most one in flight.

    function pollAllowed() {
      return shared && !stopped && !completedUrl && !authRequired &&
        typeof transport.poll === 'function' && isVisible();
    }

    function abortPoll() {
      poll.token += 1; // a late answer of the aborted request is ignored
      if (poll.deadline !== null) timers.clearTimeout(poll.deadline);
      poll.deadline = null;
      if (poll.controller) {
        try { poll.controller.abort(); } catch (err) { /* already settled */ }
      }
      poll.controller = null;
      poll.inFlight = false;
    }

    function stopPoll() {
      abortPoll();
      if (poll.timer !== null) timers.clearTimeout(poll.timer);
      poll.timer = null;
    }

    function schedulePoll(ms, wait) {
      if (poll.timer !== null) timers.clearTimeout(poll.timer);
      poll.timer = timers.setTimeout(function () {
        poll.timer = null;
        startPoll(wait);
      }, ms);
    }

    function pollFailed() {
      poll.failures += 1;
      if (pollAllowed()) schedulePoll(backoffDelay(poll.failures), true);
    }

    function startPoll(wait) {
      if (!pollAllowed() || poll.inFlight) return;
      if (poll.timer !== null) {
        timers.clearTimeout(poll.timer);
        poll.timer = null;
      }
      var token = poll.token + 1;
      poll.token = token;
      poll.inFlight = true;
      var controller = makeController();
      poll.controller = controller;
      var startedAt = now();
      var request;
      try {
        request = Promise.resolve(transport.poll({
          since: revOf(base.payload),
          wait: Boolean(wait),
          signal: controller ? controller.signal : undefined,
        }));
      } catch (err) {
        request = Promise.reject(err);
      }
      // A held request is abandoned after the server's wait + slack (from
      // the bootstrap); an instant one after the flush's timeout.
      var abortAfter = wait && longpollAbortMs ? longpollAbortMs : cfg.timeoutMs;
      var settled = new Promise(function (resolve, reject) {
        poll.deadline = timers.setTimeout(function () {
          if (controller) {
            try { controller.abort(); } catch (err) { /* settled */ }
          }
          reject(new Error('timeout'));
        }, abortAfter);
        request.then(resolve, reject);
      });
      settled.then(function (resp) {
        if (token !== poll.token) return;
        timers.clearTimeout(poll.deadline);
        poll.deadline = null;
        poll.inFlight = false;
        poll.controller = null;
        if (resp.loginRedirect || resp.status === 401 || resp.status === 403) {
          authRequired = true;
          onChange(view());
          onEvent('auth', { status: resp.status });
          return;
        }
        if (resp.status === 200 && resp.body) {
          poll.failures = 0;
          adoptRemote(resp.body);
          // Re-issued at once with the server's rev. If the base does not
          // carry that rev (the match is over with ops queued, or a base
          // that claims a rev the server never had), `since` would differ
          // again and the server answer at once again: never spin on that.
          if (!pollAllowed()) return;
          if (revOf(base.payload) === revOf(resp.body)) startPoll(true);
          else schedulePoll(cfg.busyMs, true);
        } else if (resp.status === 204) {
          poll.failures = 0;
          // `busy`: another poll of mine still holds the server's slot (a
          // reloaded page). A "held" poll that came straight back is the
          // same thing in effect. Either way, never spin.
          var quick = wait && now() - startedAt < cfg.minHoldMs;
          if (resp.busy || quick) schedulePoll(cfg.busyMs, true);
          else if (pollAllowed()) startPoll(true);
        } else {
          pollFailed();
        }
      }, function () {
        if (token !== poll.token) return;
        timers.clearTimeout(poll.deadline);
        poll.deadline = null;
        poll.inFlight = false;
        poll.controller = null;
        pollFailed();
      });
    }

    // load, visible again, `online`: an instant answer first (wait=0), then
    // the held loop. Replaces whatever poll was in flight or waiting.
    function kickPoll() {
      if (!shared) return;
      stopPoll();
      poll.failures = 0;
      startPoll(false);
    }

    // --- the public surface ----------------------------------------------

    var session = {
      view: view,

      // Apply an op locally, persist it, schedule a flush. Returns
      // {ok, op, completedGame} or {ok:false, error}.
      dispatch: function (op) {
        if (completedUrl) return { ok: false, error: 'Match is already complete' };
        if (follow) return { ok: false, error: 'Read-only: another device is scoring' };
        if (shared && op.type === 'undo') {
          // A shared undo names the point it removes (KAN-150 `expects`):
          // the sequence the trailing point has in what this phone shows,
          // fixed now, at the tap. If anything lands before it, the server
          // refuses it ("stale undo") rather than delete a point not seen.
          if (local.trailing < 1) return { ok: false, error: 'Nothing to undo' };
          op = Object.assign({}, op, { expects: local.trailing });
        }
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
        if (shared) {
          // The long-poll re-reads the server (wait=0 first); the flush
          // sends what is queued. Independent of each other.
          kickPoll();
          if (!outbox.size()) return Promise.resolve();
        }
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

      // The page went hidden: no request is held for a tab nobody looks at.
      // `nudge()` (visible again) starts it over with wait=0.
      pausePolling: function () {
        stopPoll();
      },

      stop: function () {
        stopped = true;
        if (timer !== null) timers.clearTimeout(timer);
        timer = null;
        stopPoll();
      },
    };
    onChange(view());
    if (outbox.size()) schedule(0);
    kickPoll(); // shared only: the load-time wait=0
    return session;
  }

  return Object.freeze({
    create: create,
    fetchTransport: fetchTransport,
    comparable: comparable,
    DEFAULTS: DEFAULTS,
  });
});

/*
 * scoreboard.js -- the live scoreboard's DOM layer (KAN-95, KAN-81).
 *
 * Plain JS, deliberately not Alpine: it must run under a CSP with no
 * 'unsafe-eval' (the e2e PROD_CSP), so every overlay here is verified the
 * way it ships. State lives in live_session.js; this file only:
 *
 *   - turns taps into ops (session.dispatch) and fires the tap feedback
 *     (bumpScore + the HAPTICS table) on the same frame;
 *   - renders the session's view (score, serve, pips, prompts, sync status);
 *   - wires the triggers: online, visible again, wake lock;
 *   - guards leaving with unsaved ops (beforeunload + the leave sheet);
 *   - shows the finish screen, the re-auth prompt, the follow banner and the
 *     rejected/divergence toast.
 *
 * Every overlay starts `hidden` in the markup and only this file reveals it,
 * so with scripting off nothing can render open (fail closed).
 *
 * Reports (rejections, divergence, another device, corrupt storage) go to
 * console.warn, to window.ttstatsReport(message, extra) when something
 * defines it, and as a `ttstats:report` event on window. There is no
 * client-side Sentry today; the hook is where it would plug in.
 */
(function () {
  'use strict';

  // Haptic vocabulary. Each pattern is tied to one causal event and fires on
  // the same frame as its visual, otherwise the two read as unrelated. Kept
  // short and few: a buzz on everything trains the umpire to ignore them all.
  var HAPTICS = {
    point: 12,
    gameWon: [0, 35, 70, 35],
    sideSwitch: [0, 45, 90, 45],
    error: [0, 70, 50, 70],
  };

  var MESSAGES = {
    rejected: 'The server didn’t accept a point — score reset to the saved one',
    divergence: 'The server didn’t accept a point — score reset to the saved one',
    conflict: 'This match is being scored on another device',
  };

  var FOLLOW_POLL_MS = 5000;
  var TOAST_MS = 4000;

  function readJSON(id) {
    var el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  }

  function plural(n, one, many) {
    return n + ' ' + (n === 1 ? one : many);
  }

  function report(matchPk) {
    return function (message, extra) {
      var detail = Object.assign({ match: matchPk }, extra || {});
      try {
        if (window.console && console.warn) console.warn('[scoreboard] ' + message, detail);
        if (typeof window.ttstatsReport === 'function') window.ttstatsReport(message, detail);
        window.dispatchEvent(new CustomEvent('ttstats:report', { detail: { message: message, extra: detail } }));
      } catch (err) {
        // Reporting must never break scoring.
      }
    };
  }

  function haptic(pattern) {
    if (!('vibrate' in navigator)) return;
    try {
      navigator.vibrate(pattern);
    } catch (err) {
      // Some browsers throw when the page has never been interacted with.
    }
  }

  function boot() {
    var root = document.getElementById('scoreboard');
    if (!root || !window.LiveRules || !window.LiveOutbox || !window.LiveSession) return;

    var config = readJSON('scoreboard-config');
    var bootstrap = readJSON('scoreboard-bootstrap');
    var rules = window.LiveRules.create(readJSON('live-rules-config'));
    var send = report(config.match_pk);

    function q(selector) { return root.querySelector(selector); }
    function qa(selector) { return Array.prototype.slice.call(root.querySelectorAll(selector)); }
    function show(el, visible) { if (el) el.hidden = !visible; }

    var el = {
      picker: q('[data-sb-picker]'),
      board: q('[data-sb-board]'),
      chrome: q('[data-sb-chrome]'),
      toast: q('[data-sb-toast]'),
      sideSwitch: q('[data-sb-switch]'),
      finish: q('[data-sb-finish]'),
      follow: q('[data-sb-follow]'),
      reauth: q('[data-sb-reauth]'),
      indicator: q('[data-sync-indicator]'),
      indicatorText: q('[data-sync-text]'),
      syncSheet: q('[data-sync-sheet]'),
      syncDetail: q('[data-sync-detail]'),
      syncLast: q('[data-sync-last]'),
      leaveSheet: q('[data-leave-sheet]'),
      leaveCount: q('[data-leave-count]'),
      leaveGo: q('[data-leave-go]'),
    };

    var lastView = null;
    var toastTimer = null;
    var followTimer = null;
    var leaving = false;
    var wakeLock = null;

    // --- feedback ----------------------------------------------------------

    // Restart the animation rather than letting a still-running one swallow
    // the next tap: remove, force a reflow, re-add.
    function bumpScore(side) {
      var score = q('[data-score="' + side + '"]');
      if (!score) return;
      score.classList.remove('score-bump');
      void score.offsetWidth;
      score.classList.add('score-bump');
    }

    function showError(message) {
      el.toast.textContent = message;
      show(el.toast, true);
      haptic(HAPTICS.error);
      clearTimeout(toastTimer);
      toastTimer = setTimeout(function () { show(el.toast, false); }, TOAST_MS);
    }

    // --- render ------------------------------------------------------------

    function syncWords(view) {
      if (view.phase === 'offline') return 'Offline — saved on this phone';
      var noun = view.pendingPoints === view.pending ? ['point', 'points'] : ['change', 'changes'];
      return plural(view.pending, noun[0], noun[1]) + ' not saved yet';
    }

    function render(view) {
      var previous = lastView;
      lastView = view;
      var state = view.state || {};
      var started = Boolean(state.started);

      show(el.picker, !started && view.phase !== 'follow');
      show(el.board, started);
      show(el.chrome, started);

      ['team1', 'team2'].forEach(function (side) {
        var score = q('[data-score="' + side + '"]');
        if (score) score.textContent = String(state[side + '_points'] || 0);
        var serving = view.currentServer === side;
        show(q('[data-serve-dot="' + side + '"]'), serving);
        var word = q('[data-serve-word="' + side + '"]');
        if (word) word.textContent = serving ? 'serving' : 'receiving';
        var games = state[side + '_games'] || 0;
        qa('[data-pip="' + side + '"]').forEach(function (pip) {
          if (Number(pip.getAttribute('data-pip-index')) < games) pip.setAttribute('data-won', '');
          else pip.removeAttribute('data-won');
        });
        var count = q('[data-games="' + side + '"]');
        if (count) count.textContent = String(games);
        var tally = q('[data-finish-tally="' + side + '"]');
        if (tally) tally.textContent = String(games);
      });
      // Side switch: the zones swap places, nothing else moves.
      var zone2 = q('.tap-zone[data-side="team2"]');
      var zone1 = q('.tap-zone[data-side="team1"]');
      if (zone1 && zone2) {
        zone1.style.order = state.side_switched ? '2' : '';
        zone2.style.order = state.side_switched ? '1' : '';
      }
      var gameNumber = q('[data-game-number]');
      if (gameNumber) gameNumber.textContent = String(Math.min(state.current_game_number || 1, config.best_of));

      var prompting = view.shouldPromptSideSwitch && !view.complete && view.phase !== 'follow';
      show(el.sideSwitch, prompting);
      if (prompting && !(previous && previous.shouldPromptSideSwitch)) haptic(HAPTICS.sideSwitch);

      renderSync(view);
      renderFinish(view);
      show(el.follow, view.phase === 'follow');
      show(el.reauth, view.phase === 'auth' && !view.complete);
      var reauthCount = q('[data-reauth-count]');
      if (reauthCount) {
        reauthCount.textContent = view.pending
          ? plural(view.pending, 'change is', 'changes are')
          : 'Your points are';
      }
      if (view.phase === 'follow') startFollowPoll();
      else stopFollowPoll();
      if (!view.pending && !el.leaveSheet.hidden) show(el.leaveSheet, false);
    }

    function renderSync(view) {
      var quiet = view.phase === 'synced' || view.phase === 'follow' || view.phase === 'completed' ||
        view.phase === 'auth' || view.complete;
      show(el.indicator, !quiet);
      if (!quiet) {
        el.indicatorText.textContent = syncWords(view);
        el.indicator.setAttribute('data-phase', view.phase);
        show(q('[data-sync-glyph="offline"]'), view.phase === 'offline');
        show(q('[data-sync-glyph="pending"]'), view.phase !== 'offline');
      }
      el.syncDetail.textContent = view.pending
        ? plural(view.pending, 'change is', 'changes are') + ' saved on this phone and will be sent as soon as the server can be reached.'
        : 'Everything is saved.';
      el.syncLast.textContent = view.lastSyncedAt
        ? 'Last saved ' + new Date(view.lastSyncedAt).toLocaleTimeString()
        : 'Not saved to the server since this page opened.';
      if (!view.persistent && view.pending) {
        el.syncDetail.textContent += ' This browser is not keeping them if the page closes.';
      }
    }

    function renderFinish(view) {
      show(el.finish, view.complete);
      if (!view.complete) return;
      var winner = q('[data-finish-winner]');
      if (winner) {
        var label = view.winner ? root.getAttribute('data-' + view.winner + '-label') : '';
        winner.textContent = label ? label + ' wins' : '';
      }
      var list = q('[data-finish-games]');
      var rowTemplate = q('template[data-finish-game-row]');
      if (list && rowTemplate) {
        list.textContent = '';
        view.games.forEach(function (game) {
          var row = rowTemplate.content.firstElementChild.cloneNode(true);
          row.textContent = 'Game ' + game.game_number + ': ' + game.team1 + '–' + game.team2;
          list.appendChild(row);
        });
      }
      var status = q('[data-finish-status]');
      if (status) {
        if (view.phase === 'completed') status.textContent = 'Saved. Opening the result…';
        else if (view.phase === 'auth') status.textContent = 'Sign in again to save the result. It is kept on this phone.';
        else if (view.phase === 'offline') status.textContent = 'Offline — the result is saved on this phone and will be sent when the connection returns.';
        else status.textContent = 'Saving result…';
      }
      show(q('[data-finish-retry]'), view.phase === 'offline');
      // The re-auth link stays reachable over the finish screen.
      if (view.phase === 'auth') show(el.reauth, true);
    }

    // --- session -----------------------------------------------------------

    var outbox = window.LiveOutbox.load({
      matchPk: config.match_pk,
      userId: config.user_id,
      report: send,
    });
    window.LiveOutbox.requestPersistence(navigator);

    var transport = window.LiveSession.fetchTransport({
      fetch: window.fetch.bind(window),
      syncUrl: root.getAttribute('data-sync-url'),
      stateUrl: root.getAttribute('data-state-url'),
      csrfToken: function () {
        var input = document.querySelector('input[name=csrfmiddlewaretoken]');
        return input ? input.value : '';
      },
    });

    var session = window.LiveSession.create({
      rules: rules,
      outbox: outbox,
      transport: transport,
      bootstrap: bootstrap,
      bestOf: config.best_of,
      maxBatch: config.max_batch,
      isOnline: function () { return navigator.onLine !== false; },
      report: send,
      onChange: render,
      onEvent: function (type, detail) {
        if (type === 'completed') {
          leaving = true;
          // KAN-82: drop this match's stored copy so it never reopens offline
          // as a live scoreboard of a finished match.
          try {
            var sw = navigator.serviceWorker && navigator.serviceWorker.controller;
            if (sw) sw.postMessage({ type: 'live-match-complete', url: window.location.pathname });
          } catch (e) { /* the redirect evicts it anyway */ }
          window.location.assign(detail.redirectUrl);
        } else if (MESSAGES[type]) {
          showError(MESSAGES[type]);
        }
      },
    });

    function startFollowPoll() {
      if (followTimer) return;
      followTimer = setInterval(function () {
        if (document.visibilityState === 'visible') session.refresh({ force: true });
      }, FOLLOW_POLL_MS);
    }

    function stopFollowPoll() {
      if (!followTimer) return;
      clearInterval(followTimer);
      followTimer = null;
    }

    // --- taps --------------------------------------------------------------

    function addPoint(side) {
      var view = session.view();
      if (view.shouldPromptSideSwitch || view.complete || view.phase === 'follow') return;
      var result = session.dispatch({ type: 'point', side: side });
      if (!result.ok) return;
      bumpScore(side);
      haptic(result.completedGame ? HAPTICS.gameWon : HAPTICS.point);
    }

    function act(action) {
      switch (action) {
        case 'undo':
          if (session.view().phase !== 'follow') session.dispatch({ type: 'undo' });
          return;
        case 'side-switch':
          session.dispatch({ type: 'side_switch' });
          return;
        case 'retry':
          session.nudge();
          return;
        case 'close-sheet':
          show(el.syncSheet, false);
          return;
        case 'stay':
          show(el.leaveSheet, false);
          return;
        case 'take-over':
          session.takeOver();
          return;
        default:
      }
    }

    root.addEventListener('click', function (event) {
      var target = event.target;
      var zone = target.closest('.tap-zone[data-side]');
      if (zone) {
        addPoint(zone.getAttribute('data-side'));
        return;
      }
      var start = target.closest('[data-start]');
      if (start) {
        session.dispatch({ type: 'start', initial_server: start.getAttribute('data-start') });
        return;
      }
      if (target.closest('[data-sync-indicator]')) {
        show(el.syncSheet, true);
        return;
      }
      var action = target.closest('[data-action]');
      if (action) {
        act(action.getAttribute('data-action'));
        return;
      }
      // The leave guard: a link out of the scoreboard while ops are unsaved
      // opens the sheet instead of navigating. defaultPrevented also keeps
      // base.html's navigation progress bar from arming.
      var link = target.closest('a[href]');
      if (!link) return;
      if (link.hasAttribute('data-leave-ok')) {
        leaving = true;
        return;
      }
      var view = session.view();
      if (view.pending && view.phase !== 'completed') {
        event.preventDefault();
        el.leaveCount.textContent = plural(view.pending, 'change', 'changes');
        el.leaveGo.setAttribute('href', link.getAttribute('href'));
        show(el.leaveSheet, true);
      }
    });

    // The browser's own guard, for reloads, the back gesture and typed URLs.
    window.addEventListener('beforeunload', function (event) {
      if (leaving) return undefined;
      var view = session.view();
      if (!view.pending || view.phase === 'completed') return undefined;
      event.preventDefault();
      event.returnValue = '';
      return '';
    });
    // Back from bfcache: whatever we were leaving for did not happen.
    window.addEventListener('pageshow', function () { leaving = false; });

    // --- triggers ------------------------------------------------------------

    async function acquireWakeLock() {
      if (!('wakeLock' in navigator)) return;
      try {
        wakeLock = await navigator.wakeLock.request('screen');
      } catch (err) {
        // user-denied or device unsupported -- no-op
      }
    }

    window.addEventListener('online', function () { session.nudge(); });
    window.addEventListener('offline', function () { render(session.view()); });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible') return;
      acquireWakeLock();
      // Flush what is pending, or (nothing pending) re-read the canonical
      // state: a phone that slept, or a match finished from another device,
      // would otherwise show a score the server no longer agrees with.
      session.nudge();
    });
    acquireWakeLock();

    // A test and debugging handle; nothing in the page reads it.
    root.scoreboard = { session: session, outbox: outbox };
    void wakeLock;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

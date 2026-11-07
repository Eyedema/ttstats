/*
 * live_rules.js -- the client-side mirror of pingpong/live_scoring.py (KAN-79).
 *
 * live_scoring.py is the authority. This file exists so the scoreboard can
 * roll games over, name the server and prompt the side switch with no
 * connection; it is a VERIFIED MIRROR, never a second source of the rules:
 * tests/js/live_rules.test.mjs replays the golden vectors that
 * pingpong/tests/live_vectors.py generates from the Python module and demands
 * the same state after every op. Change live_scoring.py -> regenerate the
 * vectors -> make this file match.
 *
 * No DOM, no Django, no eval. The rule constants are INJECTED, never typed
 * here: build an engine with LiveRules.create(config), where config is
 * live_scoring.rules_config() (in a page: {% live_rules_config %} from
 * live_tags, read back with JSON.parse on the json_script element).
 *
 * State has exactly the shape of Match.live_state. Every function returns a
 * new object and leaves its argument untouched. Sides are "team1"/"team2".
 * Python's (new_state, extra) tuples come back as two-element arrays.
 *
 * Loads as a classic script (sets window.LiveRules) or via require() in Node.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.LiveRules = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var REQUIRED = ['WIN_POINTS', 'MIN_LEAD', 'DEUCE_AT', 'SERVES_PER_TURN', 'SIDE_SWITCH_AT', 'MAX_EVENTS'];

  function pyRepr(value) {
    if (typeof value === 'string') return "'" + value + "'";
    if (value === null || value === undefined) return 'None';
    return String(value);
  }

  function rulesError(message) {
    var err = new Error(message);
    err.name = 'LiveRulesError';
    return err;
  }

  function create(config) {
    if (!config || typeof config !== 'object') {
      throw new TypeError('LiveRules.create needs the live_scoring.rules_config() dict');
    }
    var missing = REQUIRED.filter(function (key) {
      return !Number.isInteger(config[key]);
    });
    if (missing.length) {
      throw new TypeError('LiveRules config is missing integer ' + missing.join(', '));
    }

    var WIN_POINTS = config.WIN_POINTS;
    var MIN_LEAD = config.MIN_LEAD;
    var DEUCE_AT = config.DEUCE_AT;
    var SERVES_PER_TURN = config.SERVES_PER_TURN;
    var SIDE_SWITCH_AT = config.SIDE_SWITCH_AT;
    var MAX_EVENTS = config.MAX_EVENTS;

    function copy(state) {
      return Object.assign({}, state);
    }

    function gamesToWin(bestOf) {
      return Math.floor(bestOf / 2) + 1;
    }

    function other(side) {
      return side === 'team1' ? 'team2' : 'team1';
    }

    function initialState(bestOf) {
      return {
        best_of: bestOf,
        team1_games: 0,
        team2_games: 0,
        team1_points: 0,
        team2_points: 0,
        current_game_number: 1,
        initial_server: null,
        started: false,
        side_switched: false,
        last_point_at: null,
        events: [],
      };
    }

    function setInitialServer(state, server) {
      var next = copy(state);
      next.initial_server = server;
      next.started = true;
      return next;
    }

    function serverForGame(initialServer, gameNumber) {
      return (gameNumber - 1) % 2 === 0 ? initialServer : other(initialServer);
    }

    function hasServer(state) {
      return Boolean(state.started) && state.initial_server !== null && state.initial_server !== undefined;
    }

    // Who serves the NEXT point (not who served the last one).
    function currentServer(state) {
      if (!hasServer(state)) return null;
      var starter = serverForGame(state.initial_server, state.current_game_number);
      var t1 = state.team1_points;
      var t2 = state.team2_points;
      var rotations;
      if (t1 >= DEUCE_AT && t2 >= DEUCE_AT) {
        var deuceTotal = 2 * DEUCE_AT;
        rotations = Math.floor(deuceTotal / SERVES_PER_TURN) + (t1 + t2 - deuceTotal);
      } else {
        rotations = Math.floor((t1 + t2) / SERVES_PER_TURN);
      }
      return rotations % 2 === 0 ? starter : other(starter);
    }

    function isGameWon(t1, t2) {
      if (t1 >= WIN_POINTS && t1 - t2 >= MIN_LEAD) return 'team1';
      if (t2 >= WIN_POINTS && t2 - t1 >= MIN_LEAD) return 'team2';
      return null;
    }

    function isValidFinalScore(t1, t2) {
      if (t1 < 0 || t2 < 0) return false;
      if (isGameWon(t1, t2) === null) return false;
      var winner = Math.max(t1, t2);
      var loser = Math.min(t1, t2);
      if (winner > WIN_POINTS) return winner - loser === MIN_LEAD;
      return true;
    }

    function isMatchComplete(state) {
      var target = gamesToWin(state.best_of);
      return state.team1_games >= target || state.team2_games >= target;
    }

    function matchWinner(state) {
      var target = gamesToWin(state.best_of);
      if (state.team1_games >= target) return 'team1';
      if (state.team2_games >= target) return 'team2';
      return null;
    }

    function isDecidingGame(state) {
      return state.current_game_number === state.best_of;
    }

    function shouldPromptSideSwitch(state) {
      if (state.side_switched) return false;
      if (!isDecidingGame(state)) return false;
      return state.team1_points >= SIDE_SWITCH_AT || state.team2_points >= SIDE_SWITCH_AT;
    }

    // Returns [newState, completedGame]; completedGame is null unless the
    // point ended a game. opts.now is the timestamp string to record.
    function applyPoint(state, side, opts) {
      if (!hasServer(state)) {
        throw rulesError('Match has not started — initial_server not set');
      }
      if (isMatchComplete(state)) throw rulesError('Match is already complete');
      if (side !== 'team1' && side !== 'team2') throw rulesError('Invalid side: ' + pyRepr(side));

      var timestamp = (opts && opts.now) || new Date().toISOString();
      var next = copy(state);
      if (side === 'team1') {
        next.team1_points = state.team1_points + 1;
      } else {
        next.team2_points = state.team2_points + 1;
      }
      next.last_point_at = timestamp;

      var winner = isGameWon(next.team1_points, next.team2_points);
      var completedGame = null;
      if (winner !== null) {
        completedGame = {
          game_number: next.current_game_number,
          team1_score: next.team1_points,
          team2_score: next.team2_points,
          winner: winner,
        };
      }

      var event = { side: side, t: timestamp, game: state.current_game_number };
      if (completedGame !== null) {
        event.game_end = true;
        event.final_t1 = completedGame.team1_score;
        event.final_t2 = completedGame.team2_score;
      }
      var events = (state.events || []).slice();
      events.push(event);
      if (events.length > MAX_EVENTS) events = events.slice(events.length - MAX_EVENTS);
      next.events = events;

      if (completedGame !== null) {
        if (winner === 'team1') {
          next.team1_games = state.team1_games + 1;
        } else {
          next.team2_games = state.team2_games + 1;
        }
        next.team1_points = 0;
        next.team2_points = 0;
        next.current_game_number = state.current_game_number + 1;
      }
      return [next, completedGame];
    }

    // Returns [newState, undoneGameEnd]; undoneGameEnd is
    // {game_number, winner} when the undone point had ended a game.
    // With no events left it is a no-op.
    function undoLastPoint(state) {
      var events = (state.events || []).slice();
      if (!events.length) return [copy(state), null];

      var last = events[events.length - 1];
      var next = copy(state);
      next.events = events.slice(0, -1);

      if (last.game_end) {
        var side = last.side;
        next.team1_points = last.final_t1 - (side === 'team1' ? 1 : 0);
        next.team2_points = last.final_t2 - (side === 'team2' ? 1 : 0);
        next.current_game_number = last.game;
        if (side === 'team1') {
          next.team1_games = Math.max(0, state.team1_games - 1);
        } else {
          next.team2_games = Math.max(0, state.team2_games - 1);
        }
        return [next, { game_number: last.game, winner: side }];
      }

      if (last.side === 'team1') {
        next.team1_points = Math.max(0, state.team1_points - 1);
      } else {
        next.team2_points = Math.max(0, state.team2_points - 1);
      }
      return [next, null];
    }

    function confirmSideSwitch(state) {
      var next = copy(state);
      next.side_switched = true;
      return next;
    }

    var frozenConfig = Object.freeze(Object.assign({}, config));

    return Object.freeze({
      config: frozenConfig,
      gamesToWin: gamesToWin,
      other: other,
      initialState: initialState,
      setInitialServer: setInitialServer,
      serverForGame: serverForGame,
      currentServer: currentServer,
      isGameWon: isGameWon,
      isValidFinalScore: isValidFinalScore,
      isMatchComplete: isMatchComplete,
      matchWinner: matchWinner,
      isDecidingGame: isDecidingGame,
      shouldPromptSideSwitch: shouldPromptSideSwitch,
      applyPoint: applyPoint,
      undoLastPoint: undoLastPoint,
      confirmSideSwitch: confirmSideSwitch,
    });
  }

  return Object.freeze({ create: create });
});

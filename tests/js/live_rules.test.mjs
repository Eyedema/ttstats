// KAN-79 / KAN-92: live_rules.js must agree with live_scoring.py after every op.
//
// The vectors come from ttstats/pingpong/tests/live_vectors.py (pytest checks
// they are current). This replays every sequence through the JS engine and
// deep-equals the full state, plus current_server / should_prompt_side_switch
// / is_match_complete / match_winner and each op's return value.
//
// Run: npm run test:js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { describe, test } from 'node:test';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const RULES_PATH = new URL('../../ttstats/pingpong/static/pingpong/js/live_rules.js', import.meta.url);
const VECTORS_PATH = new URL('../../ttstats/pingpong/tests/fixtures/live_vectors.json', import.meta.url);

const LiveRules = require(RULES_PATH.pathname);
const vectors = JSON.parse(readFileSync(VECTORS_PATH, 'utf8'));
const rules = LiveRules.create(vectors.config);

function decode(steps, stateKeys) {
  const out = [];
  let flat = {};
  let events = [];
  for (const step of steps) {
    const { e, ...changed } = step;
    flat = { ...flat, ...changed };
    if (e) {
      const [drop, keep, push] = e;
      events = events.slice(drop, drop + keep).concat(push);
    }
    const state = Object.fromEntries(stateKeys.map((k) => [k, flat[k]]));
    state.events = events;
    out.push({ state, srv: flat.srv, sw: flat.sw, done: flat.done, win: flat.win, r: flat.r });
  }
  return out;
}

function runOp(state, op) {
  switch (op[0]) {
    case 'start':
      return [rules.setInitialServer(state, op[1]), null];
    case 'point':
      // A fourth element is the point's author (KAN-150), an opaque id.
      return rules.applyPoint(state, op[1], op.length > 3 ? { now: op[2], by: op[3] } : { now: op[2] });
    case 'undo':
      return rules.undoLastPoint(state);
    case 'switch':
      return [rules.confirmSideSwitch(state), null];
    default:
      throw new Error(`unknown op ${JSON.stringify(op)}`);
  }
}

function observe(state, r) {
  return {
    state,
    srv: rules.currentServer(state),
    sw: rules.shouldPromptSideSwitch(state),
    done: rules.isMatchComplete(state),
    win: rules.matchWinner(state),
    r,
  };
}

describe('live_rules.js replays the golden vectors', () => {
  test('there are at least 500 sequences', () => {
    assert.ok(vectors.sequences.length >= 500, `${vectors.sequences.length} sequences`);
  });

  for (const seq of vectors.sequences) {
    test(seq.name, () => {
      const expected = decode(seq.steps, vectors.state_keys);
      let state = rules.initialState(seq.best_of);
      assert.deepStrictEqual(observe(state, null), expected[0], `${seq.name}: initial state`);
      seq.ops.forEach((op, i) => {
        const prev = state;
        const snapshot = structuredClone(prev);
        let r;
        try {
          [state, r] = runOp(state, op);
        } catch (err) {
          r = { error: err.message };
        }
        assert.deepStrictEqual(
          observe(state, r),
          expected[i + 1],
          `${seq.name}: after op ${i} ${JSON.stringify(op)}`,
        );
        // Python copies; the mirror must not mutate the state it was given.
        assert.deepStrictEqual(prev, snapshot, `${seq.name}: op ${i} mutated its input`);
      });
    });
  }
});

describe('live_rules.js API', () => {
  test('never mutates the state it is given', () => {
    let state = rules.setInitialServer(rules.initialState(7), 'team1');
    for (let i = 0; i < 60; i++) {
      const snapshot = structuredClone(state);
      const [next] = rules.applyPoint(state, i % 3 ? 'team1' : 'team2', { now: `t${i}` });
      assert.deepStrictEqual(state, snapshot);
      state = next;
    }
    for (let i = 0; i < 60; i++) {
      const snapshot = structuredClone(state);
      const [next] = rules.undoLastPoint(state);
      assert.deepStrictEqual(state, snapshot);
      state = next;
    }
    const snapshot = structuredClone(state);
    rules.confirmSideSwitch(state);
    rules.setInitialServer(state, 'team2');
    assert.deepStrictEqual(state, snapshot);
  });

  test('isGameWon and isValidFinalScore agree with the rules on a score grid', () => {
    const { WIN_POINTS, MIN_LEAD } = vectors.config;
    for (let a = 0; a <= WIN_POINTS + 6; a++) {
      for (let b = 0; b <= WIN_POINTS + 6; b++) {
        const lead = a - b;
        const won = a >= WIN_POINTS && lead >= MIN_LEAD ? 'team1' : b >= WIN_POINTS && -lead >= MIN_LEAD ? 'team2' : null;
        assert.equal(rules.isGameWon(a, b), won, `${a}-${b}`);
        const valid = won !== null && (Math.max(a, b) <= WIN_POINTS || Math.abs(lead) === MIN_LEAD);
        assert.equal(rules.isValidFinalScore(a, b), valid, `${a}-${b}`);
      }
    }
    assert.equal(rules.isValidFinalScore(-1, WIN_POINTS), false);
  });

  test('the constants are injected: another config gives other rules', () => {
    const r21 = LiveRules.create({ ...vectors.config, WIN_POINTS: 21, DEUCE_AT: 20 });
    assert.equal(r21.isGameWon(11, 0), null);
    assert.equal(r21.isGameWon(21, 19), 'team1');
    let state = r21.setInitialServer(r21.initialState(1), 'team1');
    for (let i = 0; i < 20; i++) [state] = r21.applyPoint(state, 'team1', { now: 't' });
    for (let i = 0; i < 20; i++) [state] = r21.applyPoint(state, 'team2', { now: 't' });
    // 20-20: deuce from here, so the serve changes every point.
    const first = r21.currentServer(state);
    [state] = r21.applyPoint(state, 'team1', { now: 't' });
    assert.notEqual(r21.currentServer(state), first);
  });

  test('create() refuses a config missing a constant', () => {
    for (const key of Object.keys(vectors.config)) {
      const config = { ...vectors.config };
      delete config[key];
      assert.throws(() => LiveRules.create(config), new RegExp(key));
    }
    assert.throws(() => LiveRules.create(), TypeError);
  });

  test('config is exposed and frozen', () => {
    assert.deepStrictEqual({ ...rules.config }, vectors.config);
    assert.ok(Object.isFrozen(rules.config));
    assert.ok(Object.isFrozen(rules));
  });

  test('applyPoint stamps a timestamp when none is given', () => {
    const state = rules.setInitialServer(rules.initialState(1), 'team1');
    const [next] = rules.applyPoint(state, 'team1');
    assert.match(next.last_point_at, /^\d{4}-\d\d-\d\dT/);
    assert.equal(next.events[0].t, next.last_point_at);
  });
});

describe('live_rules.js source', () => {
  const source = readFileSync(RULES_PATH, 'utf8');
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

  test('re-types no rule constant (only 0, 1 and 2 appear as literals)', () => {
    const literals = new Set(code.match(/\b\d+\b/g));
    assert.deepStrictEqual([...literals].filter((n) => !['0', '1', '2'].includes(n)), []);
  });

  test('uses no eval, Function, DOM or Django', () => {
    for (const banned of [/\beval\s*\(/, /\bnew Function\b/, /\bdocument\b/, /\bwindow\b/, /\{%|\{\{/]) {
      assert.doesNotMatch(code, banned);
    }
  });

  test('loads as a classic browser script and sets the LiveRules global', () => {
    const sandbox = {};
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'live_rules.js' });
    assert.equal(typeof sandbox.LiveRules.create, 'function');
    const browserRules = sandbox.LiveRules.create(vectors.config);
    assert.equal(browserRules.isGameWon(11, 9), 'team1');
  });
});

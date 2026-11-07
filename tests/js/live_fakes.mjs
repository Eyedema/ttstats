// Test doubles for the scoreboard client modules (not a test file itself:
// npm run test:js only picks up *.test.mjs).
//
//   memoryStorage()  a Storage shim
//   fakeClock()      manual timers; advance(ms) runs what falls due, letting
//                    promise callbacks settle in between
//   FakeServer       live_sync's contract (seq dedupe, gap, rule rejection
//                    consuming its seq, completion + redirect_url) on top of
//                    the real LiveRules, so a test can count what it applied
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const JS = '../../ttstats/pingpong/static/pingpong/js/';
export const LiveRules = require(new URL(JS + 'live_rules.js', import.meta.url).pathname);
export const LiveOutbox = require(new URL(JS + 'live_outbox.js', import.meta.url).pathname);
// Loaded on first use, so the outbox tests do not depend on the session module.
export const loadSession = () => require(new URL(JS + 'live_session.js', import.meta.url).pathname);

const vectors = JSON.parse(readFileSync(
  new URL('../../ttstats/pingpong/tests/fixtures/live_vectors.json', import.meta.url), 'utf8'));
export const rules = LiveRules.create(vectors.config);

export function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

export function fakeClock(start = Date.UTC(2026, 10, 7, 12, 0, 0)) {
  let now = start;
  let nextId = 1;
  const timers = new Map();
  return {
    now: () => now,
    timers: {
      setTimeout(fn, ms) {
        const id = nextId++;
        timers.set(id, { fn, at: now + Math.max(0, ms || 0) });
        return id;
      },
      clearTimeout(id) { timers.delete(id); },
    },
    pending: () => [...timers.values()].map((t) => t.at - now).sort((a, b) => a - b),
    async advance(ms) {
      const target = now + ms;
      await settle();
      for (;;) {
        let due = null;
        for (const [id, t] of timers) {
          if (t.at <= target && (!due || t.at < due[1].at)) due = [id, t];
        }
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
        await settle();
        await settle();
      }
      now = target;
      await settle();
    },
  };
}

export class FakeServer {
  constructor(bestOf = 3, { initialServer = null } = {}) {
    this.bestOf = bestOf;
    this.state = rules.initialState(bestOf);
    if (initialServer) this.state = rules.setInitialServer(this.state, initialServer);
    this.applied = 0;
    this.live = true;
    this.points = []; // the point log: one entry per applied point
    this.requests = [];
    this.forceReject = new Map(); // seq -> error, a rule the mirror lacks
    this.tamper = null; // (op) -> op, to make the server disagree
  }

  payload(extra = {}) {
    const state = this.live ? this.state : {};
    return {
      is_live: this.live,
      is_match_complete: !this.live,
      state,
      current_server: this.live && state.started ? rules.currentServer(state) : null,
      should_prompt_side_switch: this.live && state.started ? rules.shouldPromptSideSwitch(state) : false,
      redirect_url: this.live ? null : '/pingpong/match/1/confirm/',
      applied_seq: this.applied,
      ...extra,
    };
  }

  applyOne(op) {
    if (this.forceReject.has(op.seq)) throw new Error(this.forceReject.get(op.seq));
    if (this.tamper) op = this.tamper(op);
    switch (op.type) {
      case 'start':
        if (this.state.started) throw new Error('Match already started');
        this.state = rules.setInitialServer(this.state, op.initial_server);
        return;
      case 'point': {
        const [next, done] = rules.applyPoint(this.state, op.side, { now: op.client_ts });
        this.state = next;
        this.points.push(op.side);
        if (done && rules.isMatchComplete(next)) this.live = false;
        return;
      }
      case 'undo': {
        const hadEvents = (this.state.events || []).length > 0;
        this.state = rules.undoLastPoint(this.state)[0];
        if (hadEvents) this.points.pop();
        return;
      }
      case 'side_switch':
        this.state = rules.confirmSideSwitch(this.state);
        return;
      default:
        throw new Error('bad op');
    }
  }

  sync(ops) {
    this.requests.push(ops.map((op) => op.seq));
    let progressed = false;
    let rejected = null;
    for (const op of [...ops].sort((a, b) => a.seq - b.seq)) {
      if (op.seq <= this.applied) continue;
      if (op.seq > this.applied + 1) {
        if (!progressed) return { status: 409, body: { error: 'gap', applied_seq: this.applied } };
        rejected = { seq: op.seq, error: 'gap' };
        break;
      }
      if (!this.live) {
        rejected = { seq: op.seq, error: 'match complete' };
        break;
      }
      progressed = true;
      try {
        this.applyOne(op);
        this.applied = op.seq;
      } catch (err) {
        this.applied = op.seq; // a rule rejection consumes its seq
        rejected = { seq: op.seq, error: err.message };
        break;
      }
    }
    return { status: 200, body: { ...this.payload(), rejected } };
  }

  stateAnswer() {
    const extra = this.live ? {} : { redirect_url: '/pingpong/matches/1/' };
    return { status: 200, body: this.payload(extra) };
  }
}

/**
 * A transport over a FakeServer whose behaviour per request can be scripted:
 * push functions onto `script`; each takes (server, ops, signal) and returns
 * the response promise. With nothing scripted the server answers normally.
 */
export function scriptedTransport(server) {
  const script = [];
  const transport = {
    script,
    calls: 0,
    sync(ops, { signal } = {}) {
      transport.calls += 1;
      const step = script.shift();
      if (step) return step(server, ops, signal);
      return Promise.resolve(server.sync(ops));
    },
    state() {
      return Promise.resolve(server.stateAnswer());
    },
  };
  return transport;
}

// Scripted behaviours.
export const lostAck = (server, ops) => {
  server.sync(ops); // applied...
  return Promise.reject(new TypeError('Load failed')); // ...but the answer never arrives
};
export const networkDown = () => Promise.reject(new TypeError('Load failed'));
export const answer = (status, body = null, extra = {}) => () =>
  Promise.resolve({ status, body, loginRedirect: false, ...extra });
export const loginBounce = () => Promise.resolve({ status: 200, body: null, loginRedirect: true });
export const hang = (server, ops, signal) => new Promise((resolve, reject) => {
  if (signal) signal.addEventListener('abort', () => reject(new Error('AbortError')));
});

export function makeSession({
  server = new FakeServer(3),
  storage = memoryStorage(),
  clock = fakeClock(),
  bootstrap = null,
  online = () => true,
  random = () => 0.5,
  matchPk = 1,
  userId = 1,
  ...rest
} = {}) {
  const transport = rest.transport || scriptedTransport(server);
  const events = [];
  const reports = [];
  const views = [];
  const outbox = LiveOutbox.load({
    storage, matchPk, userId, now: clock.now,
    report: (message, detail) => reports.push({ message, detail }),
  });
  const session = loadSession().create({
    rules,
    outbox,
    transport,
    bestOf: server.bestOf,
    bootstrap: bootstrap || server.payload(),
    timers: clock.timers,
    now: clock.now,
    random,
    isOnline: online,
    report: (message, detail) => reports.push({ message, detail }),
    onChange: (v) => views.push(v),
    onEvent: (type, detail) => events.push({ type, detail }),
    ...rest,
  });
  return { session, server, storage, clock, transport, events, reports, views, outbox };
}

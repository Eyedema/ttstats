// Test doubles for the scoreboard client modules (not a test file itself:
// npm run test:js only picks up *.test.mjs).
//
//   memoryStorage()  a Storage shim
//   fakeClock()      manual timers; advance(ms) runs what falls due, letting
//                    promise callbacks settle in between
//   FakeServer       live_sync and /live/state/ (seq dedupe per stream, gap,
//                    rule rejection consuming its seq, rev, side restriction,
//                    conditional undo, completion + redirect_url, the
//                    long-poll) on top of the real LiveRules, so a test can
//                    count what it applied. Its contract with the real
//                    endpoints is pinned by live_contract.test.mjs.
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

export const CONFIRM_URL = '/pingpong/match/1/confirm/';

/**
 * The server side of live scoring, modelled exactly (KAN-165):
 *
 *   - one stream per scorer (`as`, the scorer's key; the first scorer when
 *     omitted), each with its own applied seq. A rule rejection consumes the
 *     seq; "match complete" and a gap do not;
 *   - `rev`: +1 per applied op from any stream, never on a rejection;
 *   - a scorer without can_score_both scores only its own side;
 *   - undo `expects` (required when more than one scorer), `last_point` from
 *     the point log, attribution (`by`) on every point;
 *   - the long-poll (`poll`): 200 at once when `since` is not the rev, 204
 *     when it is and there is no `wait` (or the match is over), else held
 *     until the next applied op, or `longpollWaitMs` on the attached fake
 *     clock. One held poll per scorer; a second is 204 `busy`. Like the real
 *     server it cannot see a client abort: the slot stays held.
 */
export class FakeServer {
  constructor(bestOf = 3, { initialServer = null, scorers = null, clock = null, longpollWaitMs = 20000 } = {}) {
    this.bestOf = bestOf;
    this.state = rules.initialState(bestOf);
    if (initialServer) this.state = rules.setInitialServer(this.state, initialServer);
    this.live = true;
    this.rev = 0;
    this.log = []; // the point log: {sequence, side, by}, one per applied point
    this.requests = [];
    this.polls = [];
    this.forceReject = new Map(); // seq -> error, a rule the mirror lacks
    this.tamper = null; // (op) -> op, to make the server disagree
    this.clock = clock;
    this.longpollWaitMs = longpollWaitMs;
    this.held = new Map(); // scorer key -> the held poll
    this.scorers = (scorers || [
      { key: 'host', player_id: 1, name: 'Host', side: 1, role: 'host', can_score_both: true },
    ]).map((s) => ({ applied: 0, revoked: false, ...s }));
  }

  /** Two phones: the host on side 1, the guest on side 2, each scoring its own side. */
  static shared(bestOf = 3, options = {}) {
    return new FakeServer(bestOf, {
      ...options,
      scorers: [
        { key: 'host', player_id: 1, name: 'Hana', side: 1, role: 'host', can_score_both: false },
        { key: 'guest', player_id: 2, name: 'Gus', side: 2, role: 'guest', can_score_both: false },
      ],
    });
  }

  scorer(as) {
    if (as === undefined || as === null) return this.scorers[0];
    const found = this.scorers.find((s) => s.key === as);
    if (!found) throw new Error('no scorer ' + as);
    return found;
  }

  get isShared() { return this.scorers.filter((s) => !s.revoked).length > 1; }

  // The single-scorer tests read and set the (first) stream directly.
  get applied() { return this.scorers[0].applied; }
  set applied(value) { this.scorers[0].applied = value; }
  get points() { return this.log.map((p) => p.side); }

  payload(extra = {}, as = undefined) {
    const me = this.scorer(as);
    const state = this.live ? this.state : {};
    const last = this.log[this.log.length - 1];
    return {
      is_live: this.live,
      is_match_complete: !this.live,
      state,
      current_server: state.started ? rules.currentServer(state) : null,
      should_prompt_side_switch: state.started ? rules.shouldPromptSideSwitch(state) : false,
      redirect_url: this.live ? null : CONFIRM_URL,
      applied_seq: me.applied,
      rev: this.rev,
      scorers: this.scorers.map((s) => ({
        player_id: s.player_id, name: s.name, side: s.side, role: s.role,
        last_seen_at: null, applied_seq: s.applied, revoked: s.revoked,
      })),
      me: { side: me.side, role: me.role, can_score_both: me.can_score_both },
      last_point: last ? { ...last } : null,
      ...extra,
    };
  }

  // One op through the rules; throws the server's message on a rule rejection.
  applyOne(op, scorer = this.scorers[0]) {
    if (this.forceReject.has(op.seq)) throw new Error(this.forceReject.get(op.seq));
    if (this.tamper) op = this.tamper(op);
    switch (op.type) {
      case 'start':
        if (this.state.started) throw new Error('Match already started');
        this.state = rules.setInitialServer(this.state, op.initial_server);
        return;
      case 'point': {
        // Checked before "not started", as the server does.
        if (!scorer.can_score_both && op.side !== (scorer.side === 1 ? 'team1' : 'team2')) {
          throw new Error('not your side');
        }
        const [next, done] = rules.applyPoint(this.state, op.side, { now: op.client_ts, by: scorer.player_id });
        this.state = next;
        this.log.push({ sequence: this.log.length + 1, side: op.side, by: scorer.player_id });
        if (done && rules.isMatchComplete(next)) this.live = false;
        return;
      }
      case 'undo': {
        const trailing = this.log[this.log.length - 1];
        if (op.expects === undefined || op.expects === null) {
          if (this.isShared) throw new Error('stale undo');
        } else if (!trailing || trailing.sequence !== op.expects) {
          throw new Error('stale undo');
        }
        const hadEvents = (this.state.events || []).length > 0;
        this.state = rules.undoLastPoint(this.state)[0];
        if (hadEvents) this.log.pop();
        return;
      }
      case 'side_switch':
        this.state = rules.confirmSideSwitch(this.state);
        return;
      default:
        throw new Error('bad op');
    }
  }

  sync(ops, { as } = {}) {
    const scorer = this.scorer(as);
    this.requests.push(ops.map((op) => op.seq));
    const revBefore = this.rev;
    let progressed = false;
    let rejected = null;
    for (const op of [...ops].sort((a, b) => a.seq - b.seq)) {
      if (op.seq <= scorer.applied) continue;
      if (op.seq > scorer.applied + 1) {
        if (!progressed) return { status: 409, body: { error: 'gap', applied_seq: scorer.applied } };
        rejected = { seq: op.seq, error: 'gap' };
        break;
      }
      if (!this.live) {
        rejected = { seq: op.seq, error: 'match complete' };
        break;
      }
      progressed = true;
      try {
        this.applyOne(op, scorer);
        scorer.applied = op.seq;
        this.rev += 1;
      } catch (err) {
        scorer.applied = op.seq; // a rule rejection consumes its seq
        rejected = { seq: op.seq, error: err.message };
        break;
      }
    }
    if (this.rev !== revBefore) this.wake(); // the real server: on commit
    return { status: 200, body: { ...this.payload({}, as), rejected } };
  }

  // GET /live/state/ with no `since`.
  stateAnswer({ as } = {}) {
    return { status: 200, body: this.payload({}, as) };
  }

  // GET /live/state/?since=<rev>[&wait=1] -> {status, body, loginRedirect, busy}.
  poll({ as, since = null, wait = false } = {}) {
    this.polls.push({ as, since, wait });
    const answer = () => ({ status: 200, body: this.payload({}, as), loginRedirect: false, busy: false });
    const empty = (busy = false) => ({ status: 204, body: null, loginRedirect: false, busy });
    const parsed = since !== null && /^-?\d+$/.test(String(since)) ? Number(since) : null;
    if (parsed === null || parsed !== this.rev) return Promise.resolve(answer());
    if (!wait || !this.live) return Promise.resolve(empty());
    const key = this.scorer(as).key;
    if (this.held.has(key)) return Promise.resolve(empty(true));
    return new Promise((resolve) => {
      const hold = { resolve, answer, timer: null };
      this.held.set(key, hold);
      if (this.clock) {
        hold.timer = this.clock.timers.setTimeout(() => {
          this.held.delete(key);
          resolve(empty());
        }, this.longpollWaitMs);
      }
    });
  }

  wake() {
    const holds = [...this.held.values()];
    this.held.clear();
    for (const hold of holds) {
      if (hold.timer !== null && this.clock) this.clock.timers.clearTimeout(hold.timer);
      hold.resolve(hold.answer());
    }
  }
}

/**
 * A transport over a FakeServer whose behaviour per request can be scripted:
 * push functions onto `script`; each takes (server, ops, signal) and returns
 * the response promise. With nothing scripted the server answers normally.
 * `as` is the scorer this transport speaks for (shared tests); `pollScript`
 * scripts the long-poll the same way, taking (server, opts).
 */
export function scriptedTransport(server, { as } = {}) {
  const script = [];
  const pollScript = [];
  const transport = {
    script,
    pollScript,
    calls: 0,
    pollCalls: [],
    online: true,
    sync(ops, { signal } = {}) {
      transport.calls += 1;
      const step = script.shift();
      if (step) return step(server, ops, signal);
      if (!transport.online) return Promise.reject(new TypeError('Load failed'));
      return Promise.resolve(server.sync(ops, { as }));
    },
    state() {
      return Promise.resolve(server.stateAnswer({ as }));
    },
    poll(opts) {
      transport.pollCalls.push({ since: opts.since, wait: opts.wait });
      const step = pollScript.shift();
      if (step) return step(server, opts);
      if (!transport.online) return Promise.reject(new TypeError('Load failed'));
      // An abort rejects the client's promise; the server's hold carries on.
      return new Promise((resolve, reject) => {
        if (opts.signal) {
          if (opts.signal.aborted) { reject(new Error('AbortError')); return; }
          opts.signal.addEventListener('abort', () => reject(new Error('AbortError')));
        }
        server.poll({ as, since: opts.since, wait: opts.wait }).then(resolve, reject);
      });
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
  as = undefined,
  ...rest
} = {}) {
  const transport = rest.transport || scriptedTransport(server, { as });
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
    bootstrap: bootstrap || server.payload({}, as),
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

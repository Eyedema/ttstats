# Shared live scoring (KAN-148)

Two phones scoring one match. The epic's later stories append their sections
here. The per-scorer op streams (KAN-149) have their own record,
`docs/decisions/kan-149-per-scorer-streams.md`; the rules are in
`.claude/rules/live-scoring.md`.

## Long-poll on threaded gunicorn (KAN-168)

### Why long-poll, not WebSockets or SSE

The VPS has memory to spare, so a held request per phone is affordable. What
rules out WebSockets, Channels and async SSE is **tenancy**: the active user,
club and memberships live in `threading.local` (`ttstats/middleware.py`).
Under ASGI, sync code from different requests can run on a shared executor
thread, and a thread-local could hand one request's club to another: a
tenancy leak with no error. A long-poll is plain HTTP through the proxy and
the service worker, and a dropped one is re-issued by the existing backoff.

### Workers: gthread, 3 x 8

Sync workers would let two held phones take two-thirds of the site.
`gthread` keeps WSGI and one request per thread at a time, so the
thread-locals stay correct: `CurrentUserMiddleware` clears them on the way
in and out (`test_middleware.py::TestClearOnEntry`). Sizing is
env-configurable (`GUNICORN_WORKERS`, `GUNICORN_THREADS`).

Connections are per thread: 24 at most against Postgres's default 100 (the
`db` service is this app's alone and sets no `max_connections`), so
`CONN_MAX_AGE` stays at 60. A held request closes its connection before it
waits, so phones waiting do not count. To check on the box after deploy:
`select count(*) from pg_stat_activity where usename='pingpong'` and
`SHOW max_connections`.

Module-state audit (one request per process no longer holds): the only
request-time mutable state is `_thread_locals` (per thread) and the flag memo
on it (`flags.request_scope`, reset per request). `achievements._CHECKERS`
and `jobs._REGISTRY` are filled at import (`jobs._loaded` lazily, idempotent,
and only from commands). `server_timing` registers its wrapper per request on
the thread's own connection. Sentry SDK 2.x keeps scopes in contextvars,
isolated per request by its Django integration; `before_send` reads only the
current thread's locals.

### The endpoint

`?since=<rev>&wait=1` holds an unchanged answer up to `LIVE_LONGPOLL_WAIT`
(20 s), reading only the Redis key `live_rev_key(pk)` every 200 ms. The key is
written on commit (`live_ops.publish_rev`), once per transaction, so a waiter
never wakes to uncommitted data and a 40-op batch is one write.

- **Without Redis the loop also reads the DB every 2 s.** LocMemCache is per
  process: on a box without `REDIS_URL` (or in the e2e server with more than
  one worker) an op on another worker never reaches this worker's key.
- **An absent key is "don't know"**, never a change; the view seeds it with
  `cache.add` (never `set`: a newer value written meanwhile must win).
- **Known window:** two ops committing back to back on two threads can run
  their `on_commit` writes out of order, leaving the older rev in the key. A
  waiter whose `since` is that older rev then sleeps to its timeout (the 204
  and the instant re-issue repair it). It needs two commits within the time
  one thread takes to run its callback; a compare-and-set via a Lua script
  closes it if it is ever seen.
- **One held poll per (match, user)** via a cache marker; the second gets 204
  at once with `X-Longpoll: busy`. The ticket's rule, kept: it bounds threads.
  Its cost is that a reloaded page can be refused while the old page's
  request is still held (up to 20 s, WSGI cannot see the client hang up), so
  the client must back off about a second on `busy`, not re-issue at once.
  The alternative, a newer request taking the slot over and the older one
  exiting at its next tick, is the upgrade if that delay shows up.
- **Abort timeout** is sent as `scoreboard-config.longpoll_abort_ms` =
  `(LIVE_LONGPOLL_WAIT + live_longpoll.ABORT_SLACK) * 1000` = 25 000: the
  client's own timer already includes the 5 s slack, so it types no number.
- The service worker passes `/live/state/` through (`tests/js/sw_routes.test.mjs`).
- The e2e server runs gunicorn gthread with **one** worker: its cache is
  LocMemCache and its database SQLite, both per process.

### The path to real push: contextvars

To get true push (WebSockets / SSE under ASGI), first move the request state
off `threading.local`: `_thread_locals` becomes `asgiref.local.Local` (or
explicit `contextvars.ContextVar`s for user, session, club, memberships,
flag memo, request path), with the middleware setting and resetting tokens.
Every reader goes through `middleware.get_current_*` already, except
`observability.RequestContextFilter`, `ttstats/sentry.py` and the tests that
set `_thread_locals` by hand, so the change is concentrated but high-risk: it
is the tenancy boundary. Do it as its own story with the two-tenant isolation
suite run under an ASGI server, before any async view exists.

## KAN-150: multi-writer op rules

### Side restriction

A scorer row with `can_score_both = False` may only score for its own `side`.
Anything else is 409 `not your side`, a rule rejection: it consumes the seq
(the phone drops the op and carries on) and does not bump `rev`. A solo host
has `can_score_both = True` (KAN-149), and a row-less legacy match has no
scorer to restrict, so one-phone scoring is unaffected. The check runs before
"not started", so a wrong-side tap is named as such even before the start.

### Attribution

`PointEvent.recorded_by` (FK Player, `SET_NULL`) is set on every new point,
single-scorer matches included, so the column means something from now on:
the player of the stream that applied it (`live_ops.recorded_by`), or the
scorekeeper of a row-less match. Staff driving a solo match write through
the host's stream, so their points are the host's. Rows written before the
field existed stay null and are **never backfilled**: a guess would be
indistinguishable from a record.

The same id goes on the `live_state` event as `by`, so the state carries
attribution without a query. `live_scoring` treats it as an opaque value
(`apply_point(..., by=)`), the JS mirror passes it through the same way, and
the golden vectors gained an appended `attributed` scenario; every earlier
sequence kept its bytes. The client's reconcile compares events by side,
game and game end only, so a server event with `by` and a predicted event
without one still agree.

### Conditional undo, and who may undo

Undo deletes the trailing `PointEvent`. With two phones, "the trailing point"
is a moving target: A sees its own point #7 at the end, taps Undo, and B's
point #8 lands first. An unconditional undo then deletes B's point and keeps
A's mistake.

So an undo may carry `expects`, the sequence the client believes is trailing
(it learns sequences from the payload's `last_point: {sequence, side, by}`).
If the trailing row's sequence differs, or there is nothing to undo, the undo
is 409 `stale undo` (consumes the seq, deletes nothing). The client re-reads
the state and asks again. In a **shared** match an undo without `expects` is
refused the same way; in a single-scorer match it stays optional, so today's
client is unaffected.

**Either scorer may undo the trailing point, whoever recorded it.** The two
phones belong to the two players at the same table, and "that point was
wrong" is as often noticed by the opponent as by whoever tapped it.
Restricting undo to the recorder would force the wrong phone to act and add a
round trip to every correction. `expects` is what makes the open rule safe:
nobody can delete a point they have not seen. `by` on the event is there so a
later UI can say *whose* point an undo would remove, not to gate it.

### Start and side switch

- Two `start`s: the first to arrive wins; the second is `ALREADY_STARTED`
  (existing behaviour, consumes its seq). The losing phone rebases onto the
  server's `initial_server` (client work: S13.5).
- Two `side_switch`es: `confirm_side_switch` sets a flag, so the second is a
  no-op on the state. Both are applied ops (each bumps `rev`); nothing else
  changes. Pinned in `test_live_multiwriter.py`.

### Arrival order: the game-boundary hazard (documented, not fixed)

Points are applied in **arrival order** under the match lock. The server
cannot know the order in which rallies were actually played: two phones tap
within the same second and the network decides. Inside a game this is mostly
harmless: swapping two adjacent points of different sides changes the
intermediate scores stored on the two rows (and so streak and comeback
statistics) but not the game's final score, and the server attribution
depends only on how many points have been played.

**At a game boundary it changes who won the game.** Worked example, game 1,
side 1 leads 10-9, side 1 served first:

| Real rallies | Phone | Tap |
|---|---|---|
| rally 20: side 2 wins (10-10, deuce) | B (side 2) | point team2 |
| rally 21: side 1 wins (11-10) | A (side 1) | point team1 |

B's request is slow (a weak signal), A's arrives first:

| Arrival | Applied as | Row |
|---|---|---|
| A: team1 | 11-9: **game 1 ends, side 1 wins it** | #20, game 1, 11-9 |
| B: team2 | first point of game 2: 0-1 | #21, game 2, 0-1 |

The truth is game 1 at 11-10, still in progress. The server's record is
plausible, a `Game` row exists, and both phones show the server's state. B's
phone predicted 10-10 and now sees "game 2, 0-1": the reconcile flags a
mismatch (server wins, toast + report), which is the only signal anyone gets.
The fix is a human one: undo #21 (`expects: 21`), undo #20 (`expects: 20`,
which deletes the game row), and re-tap in the right order. Each undo needs
`expects` precisely because the other phone may be tapping meanwhile.

No reordering in this story. Options for later: the client could hold a tap
that would end a game for a short grace window, the scoreboard could ask for
confirmation of a game point when another phone tapped within N ms, or the
server could reject a game-ending point when the other stream's
`last_seen_at` shows it mid-flight. Each costs latency on every game point;
the decision waits for real two-phone usage.

### The fidelity harness for two streams

`tests/test_point_fidelity.py::TestTwoStreams` generates a points script,
splits it by side into stream A (side 1) and stream B (side 2), and delivers
it in a seeded interleaving that keeps each stream's order. Each of the 50
seeds is delivered one way, rotating through five: the single-op endpoints
with random re-sends, or `live_sync` in 1-op batches, random chunks, one
batch per run, or overlapping re-sends. Five is coprime with the starting
server (`seed % 2`) and the best-of (`seed % 3`), so every delivery meets
both servers and both lengths (a rotation by four would have given each batch
split a single starting server). Batches never span the other stream's ops,
so the arrival order is the script's.
Undos with a correct `expects`, undos with a stale one, the second `start`,
both `side_switch`es and wrong-side taps are mixed in.

The oracle is `live_scoring` replaying the same arrival order: the rows
(side, game, server, scores) and `recorded_by` must match, the match must
complete at most once, and `rev` must equal the number of applied ops. A
single-stream replay through the endpoints (one solo host, the same arrival
order) must leave the same rows apart from `recorded_by`. Two canaries prove
it fails against a writer that drops `recorded_by` and an undo that ignores
`expects`.

Cost, measured on a loaded laptop (other suites running): the fidelity file
went from ~40 s to ~55-60 s serial; the two-stream tests are ~13 s of call
time. Two deliveries per seed measured ~25 s more for the same coverage of the
server code, so the deliveries rotate instead. CI runs pytest with `-n auto`.

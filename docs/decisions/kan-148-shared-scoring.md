# Shared live scoring (KAN-148)

Two phones scoring one match. Per-story choices; the rules are in
`.claude/rules/live-scoring.md`. Per-scorer streams: `kan-149-per-scorer-streams.md`.

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

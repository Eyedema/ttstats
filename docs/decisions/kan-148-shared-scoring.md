# Shared live scoring (KAN-148)

The two players of a 1v1 match score it together, each from their own phone,
each tapping only their own points. This is the epic's record; two stories
have their own: `kan-149-per-scorer-streams.md` (the stream model in detail)
and `kan-151-152-live-lobby.md` (lobby, join flow, Today card). The rules are
in `.claude/rules/live-scoring.md` and `.claude/rules/clubs.md` (the join
link). How to turn it on: "Rollout" at the end.

## Per-scorer op streams, not one per match

Before this epic the live pipeline was single-writer: one gapless `seq` per
match (`Match.live_applied_seq`), so a second device's ops were a gap and
sent the client into read-only follow mode. Each phone is now a `LiveScorer`
row (match, player, side) with its own `applied_seq`; each stream keeps the
KAN-77 contract on its own (gapless, idempotent, checked after the lock and
before `is_live`, rule rejections consume their seq), and the streams are
merged **in arrival order** under the one `select_for_update()` lock through
the one `live_ops.apply_op`. The solo host is the one-row case of the same
model, so there is one code path (details: `kan-149-per-scorer-streams.md`).

Rejected:

- **One shared seq, with a server lock or a token handed between phones.**
  Both phones must score offline independently (KAN-76); a shared counter
  cannot be allocated offline.
- **Ordering points by client timestamp.** Phone clocks drift, and reordering
  already-applied points means rewriting the point log. Deferred to a v2
  (see the game-boundary hazard below).

## A lobby, not a match with an empty side

The "waiting for an opponent" phase is a `LiveLobby` row (host, club,
best-of, location, bearer token, expiry, status). **No `Match` exists until
someone joins**, so no manager, list, stat, rating or template ever sees a
one-sided match. The rejected alternative, creating the match first with an
empty side 2, would have leaked everywhere: every one of those assumes two
sides (`set_match_sides`, `MatchCreateView`). Details, including **the one
place an unverified account is logged in** (a signup that holds an open
lobby token in a club it belongs to, so the joiner is not blocked on email
verification), are in `kan-151-152-live-lobby.md`.

The QR is rendered on the server as inline SVG (`segno`, `qr_tags.qr_svg`):
it works with JS off and has nothing to allow in CSP. A client-side QR
library would add JS on slow 4G and fail open without it.

## Long-poll on threaded gunicorn (KAN-168)

### Why long-poll, not WebSockets or SSE

The VPS has memory to spare, so a held request per phone is affordable. What
rules out WebSockets, Channels and async SSE is **tenancy**: the active user,
club and memberships live in `threading.local` (`ttstats/middleware.py`).
Under ASGI, sync code from different requests can run on a shared executor
thread, and a thread-local could hand one request's club to another: a
tenancy leak with no error. A long-poll is plain HTTP through the proxy and
the service worker, and a dropped one is re-issued by the existing backoff.

Short polling every 1-2 s was the simpler option, but it is up to 1.5 s late
and a request per phone every 1.5 s even when nothing happens; with memory to
spare, holding a request is the better trade.

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

What v1 gives the players instead: presence on the other phone ("last seen
25 s ago", KAN-154), the server's score always visible, and undo with
`(sequence, by)` to repair it.

**The v2 idea (not in this epic): order by `client_ts` with a per-stream
clock offset.** Every op already carries `client_ts` (stored as
`PointEvent.client_recorded_at`). Each sync gives the server a sample of
`server_now - client_ts` for that stream; the minimum over recent samples
estimates the phone's clock offset plus its fastest one-way delay. Ordering
by the corrected time would put B's 10-10 before A's 11-10 in the example.
Its costs: a late point has to be inserted *before* points already applied,
which means re-running `live_scoring` from that point and rewriting the
trailing `PointEvent` rows (and possibly un-ending a game), all of which the
point log was designed never to do; and a phone that was offline for a whole
game gives no fresh offset samples, exactly when ordering matters most.

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

## KAN-153: co-scoring client

The rules are in `.claude/rules/live-scoring.md` ("Shared mode"); this
records the choices behind them.

### One flag, inert when false

`scoreboard-config.shared` is `match.is_shared` (more than one non-revoked
scorer row). Every shared behaviour in `live_session.js` is keyed on it, so a
single-scorer page runs exactly the KAN-80/81 client: the unmodified
`live_session.test.mjs` passes and the e2e network log shows no `?since=`
request. The "my side" layout is a narrower condition (shared **and** this
phone lacks `can_score_both`), so a takeover (KAN-154) gets the two zones back
while still rebasing.

### Ordering two answers: `(rev, applied_seq)`

The flush and the long-poll are independent, so their answers can arrive in
either order. `rev` alone cannot order two payloads with the same `rev`
(a rule rejection consumes my seq without bumping `rev`), and `applied_seq`
alone ignores the other phone. Both only ever grow on the server, so the
pair, compared lexicographically, orders any two answers in time. An older
answer is never adopted; if it was a flush answer, only its `rejected` is news.

### "Server wins" only when nobody else moved

With two streams the server's state interleaves the other phone's ops with
mine in an order the client cannot know, so comparing it with my prediction
would cry wolf on every overlap. The client compares only when the sum of the
other scorers' `applied_seq` (from `scorers`) is unchanged between the old
base and the answer; otherwise it rebases silently. The cost: a real mismatch
in my own ops that coincides with the other phone's activity goes unreported.
The ticket's alternative ("`rev - my applied delta` equals the opponent's
applied count") cannot work, because rule rejections move `applied_seq` but
not `rev`.

### Undo `expects` for a point still in my outbox

`expects` is fixed **at the tap**, never recomputed at send time: it is the
trailing sequence the phone shows then, i.e. the base's
`last_point.sequence`, +1 for each pending point and -1 for each pending undo
before it. For an undo of my own queued point, that is the sequence the point
*will* get if nothing else lands first. Two outcomes, both safe:

- Nothing lands between my point and my undo (the usual case: both go in one
  batch, applied under one lock): the sequences match, the point is deleted.
- The other phone's point lands first: my point gets a later sequence, the
  undo is `stale undo`, nothing is deleted, the toast says so, and the
  scorer sees their point (now trailing, with its real sequence) and can
  undo again.

Recomputing at send time would be wrong in the other direction: an undo of a
*server* point tapped before the opponent's next point arrived would then
name the opponent's point, which the scorer never saw. Fixing the number at
the tap is what keeps "nobody deletes a point they have not seen". In the
local replay, a pending undo whose `expects` no longer matches is skipped,
exactly as the server will refuse it, so the screen does not show an undo
that will not happen. A queued undo is never cancelled.

Known limit (server side, KAN-150): sequences are reused after an undo, so
`expects` cannot tell "my #8" from "their new #8" when the other phone undid
#8 and scored again between my seeing it and my undo landing. It needs an
undo and a point from the other phone inside one round trip of mine; a
`(sequence, by)` check would close it. Closed by KAN-154 (`expects_by`,
below), except for one recorder re-scoring its own number.

### Follow mode is per user, and gaps renumber

Another scorer's ops move `rev`, never my stream, so follow mode now means
only "my stream's `applied_seq` is past what this outbox issued and what this
page knew": the same user on another device. A per-stream gap means ops this
outbox issued are gone from storage, not "someone else is scoring", so after
the retry the pending ops are renumbered from `applied_seq + 1` and sent
(reported), instead of turning the tab read-only.

### The long-poll loop

One request in flight, its own AbortController and timers, so a held poll
never delays a flush (pinned with the fake clock). `wait=0` on load, visible
and `online`, then `wait=1` re-issued at once on 200 and 204. `busy` (the
previous page's poll still holds the server's slot) and a "held" 204 that
came back in under a second wait `busyMs` (1 s): the server cannot see a
client abort, so a reload would otherwise spin on `busy` for up to 20 s. A
200 that did not move the base to its `rev` (a finished match with my ops
still queued) also waits, or `since` would differ forever and the loop spin.
Errors share the flush's 1/2/5/10/30 s + jitter. The abort timeout is the
bootstrap's `longpoll_abort_ms`, never a JS literal.

### Completion lands on match_confirm on both phones

`LiveStateView` used to hand a finished match's `redirect_url` as
match_detail while `live_sync` handed match_confirm. The phone that did not
tap the final point learns of the end from the long-poll, so the two now
agree: match_confirm whenever the match has a winner, match_detail only for a
match closed without one.

### The fake server has a contract

`tests/js/live_fakes.mjs` models per-stream seqs, `rev`, the side
restriction, `expects`, `last_point`, attribution and the long-poll (held
until the next applied op or the wait on the fake clock, one slot per scorer,
`busy`, and, like the server, blind to client aborts). Two scripts in
`pingpong/tests/live_contract.py` run through the real endpoints
(`test_live_contract.py`) and through the fake (`live_contract.test.mjs`);
the normalised answers (timestamps dropped, player ids as roles) must be
identical, and the pytest fails when the committed fixture is stale. Held
waits are timing and stay out of the fixture.

### Things found on the way

- `.score-bump` (the tap's numeral bump) lived in `@layer components` with a
  name only JS uses, so Tailwind purged it and it never animated. It moved
  outside `@layer` with the new `.score-pulse`.
- Playwright's `use.reducedMotion` does not reach `matchMedia` in WebKit (as
  `momentum.reduced.spec.js` notes); the reduced spec calls `emulateMedia`.

## KAN-154: presence, takeover and recovery

The rules are in `.claude/rules/live-scoring.md` ("Presence, takeover,
removal"); this records the choices.

### Undo names its recorder: `(sequence, by)`

KAN-153's known limit, closed. `PointEvent.sequence` is `MAX + 1`, so after
an undo the next point reuses the number: phone A sees B's #8, B undoes it
and scores a new #8, and A's undo of "#8" (sent with `expects` alone) would
delete a point A never saw. An undo now also carries `expects_by`, the player
the client believes recorded the trailing point, and `check_undo_expects`
refuses `stale undo` when the trailing row's `recorded_by` differs.

- The client knows the recorder of the base's `last_point` (`by`) and of its
  own queued points (itself, `me.player_id`). Below the base's trailing
  point it does not, and that undo goes out with `expects` alone. A pending
  undo whose recorder no longer matches is skipped in the local replay, as
  the server will refuse it.
- `expects` alone stays valid: older cached pages send only it, and a row
  written before KAN-150 has no `recorded_by` (`by` null, so nothing to send).
- **Remaining window:** the same recorder undoing and re-scoring the same
  number (B's old #8, B's new #8) is still indistinguishable. It needs B to
  undo its own point and score again within one round trip of A's undo; a
  per-row identity in `last_point` (the row id) would close it, at the cost
  of a field every client and the fake must carry. Not done until seen.
- The two-stream fidelity harness sends `expects_by` on half its correct
  undos and adds a "right sequence, wrong recorder" stale case; a canary
  proves it fails against a check that ignores the recorder.

### Takeover is an op, removal is not

`take_over` / `release` are sequenced in the requester's stream because they
must be ordered with that phone's points: a takeover tapped offline, then
points for the other side, must land in that order or the points are `not
your side`. Queued, they are also never lost. Removal is the host's online
decision about *another* phone; replaying it from an outbox after the fact
would be surprising, so it is a plain POST under the match lock, with no
seq.

- A takeover never touches the other row. If that phone was only in a dead
  zone, its queued points land on return (arrival order), and both phones'
  presence then shows that both score: the decision record's game-boundary
  hazard applies to them like to any two writers.
- `take_over` when already scoring both (a solo host) is applied with no rev
  bump: nothing anyone renders changed. `release` by the only active scorer
  is refused: nobody could then score the other side.
- Single-op endpoints: not added. They exist for pages cached from before
  KAN-88, which never send these ops.

### Removed is its own 403

A removed phone's requests are 403 `{"error": "removed"}` (JSON), so the
client can tell it from a CSRF failure (Django's HTML 403) and from a
session bounce. The transport's CSRF retry-once skips it, and the session
enters a `removed` phase: loops stop, taps are ignored, the outbox stays
(it is the user's data; nothing deletes it). The scoreboard page itself
redirects a removed scorer to Today with the message; match detail is not
an option while the match is live (the default manager hides live matches).
Staff keep their viewing bypass even with a revoked row of their own.

### Presence without a write per poll

`last_seen_at` is stamped at most every `SEEN_EVERY` (5 s) and only when a
request arrives, and a long-poll holds for 20 s, so a healthy phone's stamp
can be 25 s old. Lowering the throttle would turn every poll into a write;
instead the payload says `polling: true` for a scorer whose
`live_poll_key` marker is set (one `get_many`, no query): a phone holding a
poll is present. When the phone dies, its held request ends within one hold
and the marker goes, so "not seen" is at most a hold late.

The other half: a phone learns presence only from answers, and a held poll
with nothing new ends in a bodyless 204. So a 204 after a full hold is
followed by one plain read (no `since`), at most one extra request per hold
per phone. Age is measured on the server's clock (`server_now -
last_seen_at`) and then advanced by the phone's own clock since the answer
arrived, so neither clock's offset matters.

### Today for every scorer

`_live_matches` reads scorer rows (non-revoked), plus the row-less legacy
shape for its scorekeeper, and words the score from the viewer's side, so the
guest's card is "3-1 up on Host", not the host's view. The link reads "You're
scoring this match · Resume" (existing specs find it by "Resume").

### e2e: timings and rate limits

`settings/e2e.py` sets the hold to 6 s and presence to 6 s / 20 s (env
`E2E_LONGPOLL_WAIT`, `E2E_PRESENCE_IN_SYNC`, `E2E_PRESENCE_LOST`), so the
takeover spec waits about 30 s, not two minutes. IN_SYNC stays above
`SEEN_EVERY`, or a phone between two polls flickers to "last seen". The e2e
server also turns `django-ratelimit` off: the join link (20/h) and the login
form (5/15 min) are per IP, every spec request is 127.0.0.1, and
`--repeat-each=10` alone exceeds both; the limits are pytest's.

## Answers to the epic's open questions

1. **Does finishing a co-scored match count as both players confirming?**
   No. The confirmation rule is unchanged: both phones land on match_confirm
   and each verified player confirms as before (unverified players still
   auto-confirm). Changing it is the owner's call and a separate story.
2. **A guest joining without an account?** Not now. The joiner logs in or
   signs up (and may play unverified, see `kan-151-152-live-lobby.md`). A
   token-scoped scoring right without a login is a possible later story.
3. **Lobby expiry:** 30 minutes (`LOBBY_TTL`), read lazily; no job flips it.
4. **Can the host start alone and let the opponent join mid-match?** No. The
   match is created only when someone joins. The lobby's "Pick opponent
   instead" cancels it and falls back to today's single-scorer form, with
   best-of and location prefilled (`MatchCreateView.get_initial`).

## Rollout

The `shared_scoring` flag (`settings.FEATURE_FLAGS`, default False) gates
only **creating** a lobby (the match form's button, `LiveLobbyCreateView`)
and the viewer's Today card; an open lobby, the join link and the scoreboard
work whatever the joiner's flag says, so turning it on for one host is
enough to test both phones (the opponent joins by QR or link: without the
flag they do not see the Today card). `dcp` / `dweb` are the aliases in `docs/tenancy-rollout.md`.

### Before deploying: baseline

The gthread workers (KAN-168) ship with this branch and change the whole
site's concurrency, so record the "before" on the box first:

```bash
docker stats --no-stream $(dcp ps -q web)                 # web memory
# select count(*) from pg_stat_activity where usename='pingpong'
# ($POSTGRES_USER is pingpong, so current_user saves a level of quoting):
dcp exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select count(*) from pg_stat_activity where usename = current_user"'
dcp exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW max_connections"'   # 100
```

Deploy, then repeat both after a few minutes of normal traffic and again
during the real-device match. Expect at most 24 app connections (3 x 8),
plus the psql session itself;
held long-polls close theirs before waiting. Rollback of the worker model
alone: `GUNICORN_THREADS=1` in `.env.prod`, `dcp up -d web`.

### Step 1: on for the owner only

No deploy, no restart (an override takes effect on the next request):

```bash
dweb shell -c "
from django.contrib.auth.models import User
from pingpong.models import FeatureFlagOverride
u = User.objects.get(username='YOUR_USERNAME')
FeatureFlagOverride.objects.update_or_create(
    name='shared_scoring', user=u,
    defaults={'enabled': True, 'note': 'KAN-156 shared scoring, owner phase'})
"
dweb shell -c "
from django.contrib.auth.models import User
from pingpong import flags
print(flags.is_enabled('shared_scoring', User.objects.get(username='YOUR_USERNAME')))
"   # True
```

Or in the admin: Feature flag overrides, add `shared_scoring` for the user,
enabled. **Rollback:** delete the row.

### Step 2: the real-device check (record results on KAN-156)

Two phones at the venue, both with the app installed as a PWA, against prod:

- [ ] **QR -> PWA or Safari?** Scan the lobby QR with the iPhone camera app
      and record which opens. If Safari: its cookie jar is not the PWA's, so
      check that log in -> join still lands on the scoreboard, and note the
      result in this record.
- [ ] **Time to first point**: from "show QR" on the host to the first point
      applied (success criterion: under 30 s, no typing if the opponent is
      already logged in).
- [ ] **Tap-to-other-phone latency, p50 and p95 over a game** (target p50 <
      500 ms, p95 < 2 s on venue 4G).
- [ ] **One phone in airplane mode for a whole game**, then reconnected: its
      points arrive exactly once and the final score matches what was tapped.
- [ ] **Force-close one app** (battery-dead simulation): the other phone shows
      it lost and takes over ("Score for both"); the match finishes.
- [ ] **Server**: `docker stats` web memory and the `pg_stat_activity` count
      (above) during the match, against the baseline.
- [ ] Both phones land on match_confirm; ratings move only after both confirm.

### Step 3: on for everyone

```bash
# .env.prod: add to the existing comma-separated list
TTSTATS_FLAGS_ON=...,shared_scoring
```

```bash
dcp up -d web
dweb shell -c "from pingpong import flags; print(flags.is_enabled('shared_scoring'))"   # True
```

Then delete the owner's override (it no longer does anything) and watch
Sentry for a week (success criterion 5: no new errors from the threaded
workers). **Rollback:** remove `shared_scoring` from `TTSTATS_FLAGS_ON`,
`dcp up -d web`. Matches already shared keep working; only new lobbies stop.

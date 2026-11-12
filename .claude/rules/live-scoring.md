---
paths:
  - "ttstats/pingpong/live_scoring.py"
  - "ttstats/pingpong/point_stats.py"
  - "ttstats/pingpong/point_rollups.py"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/live_ops.py"
  - "ttstats/pingpong/live_longpoll.py"
  - "ttstats/pingpong/tests/test_live_longpoll.py"
  - "ttstats/pingpong/services.py"
  - "ttstats/pingpong/tests/test_live_scorers.py"
  - "ttstats/pingpong/tests/test_live_multiwriter.py"
  - "ttstats/pingpong/management/commands/rebuild_point_rollups.py"
  - "ttstats/pingpong/management/commands/point_storage.py"
  - "ttstats/pingpong/templates/pingpong/scoreboard.html"
  - "ttstats/pingpong/templates/pingpong/match_detail.html"
  - "ttstats/pingpong/templates/pingpong/player_detail.html"
  - "ttstats/pingpong/templates/pingpong/head_to_head.html"
  - "ttstats/pingpong/templates/pingpong/game_form.html"
  - "ttstats/pingpong/tests/test_point*.py"
  - "ttstats/pingpong/static/pingpong/js/live_rules.js"
  - "ttstats/pingpong/static/pingpong/js/live_outbox.js"
  - "ttstats/pingpong/static/pingpong/js/live_session.js"
  - "ttstats/pingpong/static/pingpong/js/scoreboard.js"
  - "ttstats/pingpong/templatetags/live_tags.py"
  - "ttstats/pingpong/tests/live_vectors.py"
  - "ttstats/pingpong/tests/test_live_vectors.py"
  - "ttstats/pingpong/tests/test_scoreboard.py"
  - "ttstats/pingpong/tests/fixtures/live_vectors.json"
  - "tests/js/live_*.mjs"
  - "tests/e2e/momentum*.js"
  - "tests/e2e/point-stats*.js"
  - "tests/e2e/scoreboard*.js"
  - "ttstats/pingpong/live_lobby_views.py"
  - "ttstats/pingpong/templates/pingpong/live_lobby.html"
  - "ttstats/pingpong/templates/pingpong/live_join.html"
  - "ttstats/pingpong/templatetags/qr_tags.py"
  - "ttstats/pingpong/tests/test_qr_tags.py"
  - "ttstats/pingpong/tests/test_shared_scoring.py"
  - "ttstats/pingpong/tests/fixtures/live_contract.json"
  - "ttstats/pingpong/static/pingpong/js/live_lobby.js"
  - "tests/e2e/shared-scoring*.js"
  - "tests/e2e/shared-scoreboard*.js"
  - "ttstats/pingpong/tests/live_contract.py"
  - "ttstats/pingpong/tests/test_live_contract.py"
  - "ttstats/pingpong/tests/test_scoreboard_shared.py"
  - "ttstats/pingpong/tests/test_live_takeover.py"
  - "ttstats/ttstats/settings/e2e.py"
---

# Live scoring and the point log

## Scoring rules

- `live_scoring.py` owns them: `WIN_POINTS` (11), `MIN_LEAD` (2), `is_game_won`,
  `is_valid_final_score`, `common_final_scores`. Pure, no Django.
- `is_valid_final_score` is stricter than "someone is ahead": 11-10 isn't over,
  and 13-5 can't happen.
- Serve rotation and the side switch are named too: `DEUCE_AT`,
  `SERVES_PER_TURN`, `SIDE_SWITCH_AT`. `rules_config()` is the dict of every
  constant the client needs.
- `GameForm.clean()` defers to it; the Alt+1..4 presets in `game_form.html` come
  from `common_final_scores()` via `json_script`.

## `live_rules.js` is a verified mirror, never a second authority

`static/pingpong/js/live_rules.js` ports the rules for offline scoring (KAN-79).

- **Change `live_scoring.py` -> regenerate the vectors -> fix the JS**:
  `cd ttstats && ../.venv/bin/python -m pingpong.tests.live_vectors`, then
  `npm run test:js`. `test_live_vectors.py` fails on a stale
  `tests/fixtures/live_vectors.json`; `tests/js/live_rules.test.mjs`
  deep-equals the full state after every op (CI runs both).
- **Constants are injected, never typed in JS**: `LiveRules.create(config)`
  with `config` = `rules_config()`, which the template tag
  `{% live_rules_config %}` (`{% load live_tags %}`) emits via `json_script`
  as `#live-rules-config`. The JS test fails on any numeric literal but 0/1/2.
- An event may carry `by` (who recorded the point, KAN-150): an opaque
  pass-through (`apply_point(..., by=)` / `applyPoint(state, side, {by})`),
  read by no rule. Vector ops with an author are `["point", side, now, by]`.
- Same state shape as `Match.live_state`; functions never mutate their input;
  Python's tuples come back as `[state, extra]`; refused ops throw with
  Python's message.
- UMD: a classic script sets `window.LiveRules`, Node `require()`s it. No DOM,
  no eval.

## The scoreboard opts out of pull to refresh

`#scoreboard` carries `data-no-pull-refresh` (KAN-126): a reload mid-rally
would lose taps, so `pull_to_refresh.js` attaches nothing there. Keep it on
the root (`test_scoreboard.py::TestScoreboardPage`).

## The point log is the record; `live_state` never was

`Match.live_state["events"]` is capped at `live_scoring.MAX_EVENTS`, exists only
for Undo, and is nulled at match end. `PointEvent` is the durable record: **one
row per point, written when the point is applied**, never reconstructed later.

- **`serving_side` is read from the state BEFORE `apply_point()`.** Afterwards
  `current_server()` names who serves *next*, and the error is undetectable in
  stored data. `live_ops.record_point_event` takes `state_before` explicitly.
- **Scoring wins over recording.** The write runs in its own savepoint inside
  the op's transaction and swallows everything. The savepoint matters
  on Postgres, where an error poisons the whole transaction.
- **Undo deletes the trailing row** -- never a compensating "undone" event that
  every aggregation would have to filter. A failed delete rolls the request
  back. Deleting the trailing row keeps `sequence` gapless.
- **Undo guards on `had_events`**: the in-memory log is capped and the persisted
  one isn't, so without it undo past the cap deletes points never undone.
- `sequence` is `MAX(sequence) + 1` under the existing `select_for_update()` lock.
- **`recorded_by` is set on every new point** (KAN-150): the stream's player
  (`live_ops.recorded_by`; the scorekeeper on a row-less match), also stored
  as the event's `by`. Rows from before it stay null; never backfill them.
- **`Side.from_live()` / `Side.to_live()` are the only translation** between
  `"team1"`/`"team2"` and `Side`.
- **`tests/test_point_fidelity.py` is the guard.** It replays generated points
  and undos through the real endpoints, alternating the starting server with
  the seed (a harness always starting with side 1 can't see an ignored
  `initial_server`), and carries tests proving it fails against broken writes.
  It also replays every script with duplicate re-sends and through
  `live_sync` in four batch splits (1-op, random chunks, one batch,
  overlapping re-sends): same rows, exactly one completion.
  **Two streams** (KAN-150, `TestTwoStreams`, 50 seeds): A's and B's points
  interleaved with stale/correct `expects`, a losing `start`, both
  `side_switch`es and wrong-side taps; one delivery per seed (single-op with
  re-sends or a batch split, rotating by 5). The oracle is `live_scoring` on
  the same arrival order: rows incl. `recorded_by`, one completion, `rev` =
  applied ops. Canaries: a writer dropping `recorded_by`, an undo ignoring
  `expects` (`live_ops.check_undo_expects`).
- **No starting server means no point.** `apply_point()` raises unless the state
  is `started` **and** names `initial_server`; `live_ops` rejects the point (409) before
  writing. `PointEvent.serving_side` stays nullable only as a defensive fallback.
- **Historic matches have no point rows and never will.** Every consuming
  surface must be absent rather than empty.
- Retention policy (keep everything, revisit at a threshold):
  `docs/decisions/point-event-retention.md`.

## `live_ops.apply_op` is the one way an op is applied (KAN-87)

`live_ops.py` (Django layer; rules still from `live_scoring`) applies `start`,
`point`, `undo`, `side_switch`, `take_over`, `release` and returns an `OpResult`. The four single-op
views and `live_sync` are thin wrappers: parse, lock, dedupe, call, shape. The
caller owns the transaction and the `select_for_update()` lock, so a point
synced in a batch runs exactly the code a point tapped online runs.

## Every op carries a seq; dedupe before `is_live` (KAN-77)

Ops (`start`, `point`, `undo`, `side_switch`, `take_over`, `release`)
carry a client `seq`: **per
scorer** (KAN-149), from 1, gapless. Each phone is a `LiveScorer` row whose
`applied_seq` is the highest applied; a match with no rows is the legacy
shape, whose stream is `Match.live_applied_seq` and whose only scorer is
`scorekeeper`. Both are columns because `live_state` is nulled at match end
and the most dangerous duplicate is a retried match point.

- **One lock serializes every stream**: the match row `select_for_update()`,
  then the scorer rows are re-read (`live_ops.load_scorers`) and the caller
  re-authorized under it.
- **New live matches come from `services.create_live_match`**, which writes
  the host row (`can_score_both=True` for a solo scorer). The host's stream is
  also mirrored into `live_applied_seq` for one release (rollback safety).
- The scorer row is written **once per request** (`live_ops.save_scorer`:
  consumed seq + `last_seen_at`, stamped at most every 5 s), in the op's
  transaction, so the `live_sync` ceiling did not move.
- `Match.live_rev` is +1 per op that changed the state, from any stream;
  duplicates, gaps **and rule rejections** leave it alone.
- Access: a non-revoked scorer row (or `scorekeeper` on a row-less match), or
  staff. Revoked or row-less participant is 403; staff without a row writing
  to a shared match is 409 `not a scorer`.
- In a shared match an op **without** `seq` is 409 `{"error": "reload"}`.
- **Check the seq after the lock + `refresh_from_db()` and BEFORE `is_live`**
  (`_check_seq`), or a re-sent match point gets "Match is not live" instead of
  the completion and its `redirect_url`.
- `seq <= applied`: duplicate, apply nothing, answer the canonical payload.
  `seq > applied + 1`: 409 `{"error": "gap", "applied_seq": N}`.
- **A rule rejection consumes its seq** (the client drops the op and carries
  on); an op refused because the match is not live does not.
- Undo is an op with its own seq, never a decrement.
- **A scorer without `can_score_both` scores only their own side**: anything
  else is 409 `not your side`, a rule rejection (consumes the seq). Solo hosts
  have `can_score_both=True`.
- **Conditional undo** (KAN-150): an undo may carry `expects`, the
  `PointEvent.sequence` it believes is trailing; a mismatch (or nothing to
  undo) is 409 `stale undo`, a rule rejection. **Required in a shared match**
  (missing = stale), optional on a solo one. Either scorer may undo the
  trailing point, whoever recorded it. `expects` must be an int >= 1 (400).
  **`expects_by`** (KAN-154, optional, a player id): the trailing row's
  `recorded_by` must match too, because sequences are reused after an undo.
  `expects` alone stays valid (older clients); the client sends both.
- **`take_over` / `release`** (KAN-154) set / clear `can_score_both` on the
  **requester's own** row; never the other scorer's (its queued points still
  land). Sequenced and deduped like any op; a no-op (already so) consumes the
  seq without a rev; `release` by the only active scorer is `only scorer`, a
  row-less match is `not a scorer` (both rule rejections). Batch path only
  (`live_sync`): the single-op endpoints serve pre-KAN-88 pages that never
  send them. Authorization, not scoring: never in `live_rules.js` or vectors.
- **Remove a phone** (KAN-154): `POST live/scorers/<player_pk>/remove/`, host
  only (403 `host only`), under the match lock: `revoked_at` on the guest,
  `can_score_both` on the host, one rev bump (wakes held polls). Not an op.
  A removed scorer gets **403 `{"error": "removed"}`** on every live endpoint
  (the scoreboard page redirects to Today with a message); staff keep viewing.
- Two `start`s: the first wins, the second is `ALREADY_STARTED`. Two
  `side_switch`es both apply (it sets a flag). Points apply in **arrival**
  order: `docs/decisions/kan-148-shared-scoring.md`.
- `seq` must be an int >= 1; bools and strings are 400.
- No `seq` is the legacy contract, applied unconditionally and logged. Remove
  once no cached page sends it.
- `_scoreboard_payload()` carries `applied_seq` (the caller's stream, 0 before
  any sequenced op), `rev`, `scorers` (presence: `last_seen_at`,
  `can_score_both`, `polling` = holds a long-poll now), `me` (`player_id`,
  side, role, `can_score_both`; null without a row), `server_now`, and
  `last_point` (`{sequence, side, by}` of the trailing row, or null): what a
  conditional undo sends as `expects` / `expects_by`.
  Choices: `docs/decisions/kan-149-per-scorer-streams.md`.

## `/live/state/`: instant answer, 204, or a held long-poll (KAN-165, KAN-168)

`GET matches/<pk>/live/state/[?since=<rev>[&wait=1]]`. Authorization and the
club check run **before** any wait; `last_seen_at` is still stamped.

- No `since`, or a `since` that is not an int: 200, full payload.
- A finished match's `redirect_url` is **match_confirm**, as `live_sync`'s
  (KAN-153: the phone that did not tap the final point learns it here);
  match_detail only for a match closed without a winner.
- `since != rev`: 200 at once. `since == rev` without `wait=1` (or on a
  finished match): **204, no body**.
- `wait=1`: held up to `settings.LIVE_LONGPOLL_WAIT` (20 s), then 204. The
  loop (`live_longpoll.wait_for_change`, pure, no Django) reads only
  `cache_utils.live_rev_key(pk)` every `TICK` (200 ms); on a change the view
  re-reads and re-authorizes, then answers the payload.
- **The loop never touches the DB on a shared cache.** On LocMemCache (dev,
  tests) it also re-reads `live_rev` every `DB_EVERY` (2 s), since another
  worker's op never reaches this process's key. A key reading `None` is
  "don't know", never a change; the view seeds an absent key with `add()`.
- **The connection is closed before waiting** (`_release_db_connection`,
  skipped inside an atomic block), so held phones don't pin connections.
- **One held poll per (match, user)**: `live_poll_key` marker via
  `cache.add`; a second concurrent one is 204 at once with
  `X-Longpoll: busy` (the client should back off ~1 s on it, not spin).
- **At most `LIVE_LONGPOLL_MAX_HELD` held polls per process**
  (`GUNICORN_THREADS` - 3 = 5, env-overridable, min 1): a non-blocking
  `threading.BoundedSemaphore` (`views._held_polls`); full is the same
  instant busy 204, released in `finally`. Held phones never take every
  thread, so the ops they wait for still run.
- **The key is written on commit, never before** (`live_ops.publish_rev`,
  registered once per transaction from `_write_state`, so a batch is one
  write with the final rev). A dead cache never fails a committed op.
- The client's abort timeout is `scoreboard-config.longpoll_abort_ms` =
  `(LIVE_LONGPOLL_WAIT + live_longpoll.ABORT_SLACK) * 1000`; never typed in JS.
- The service worker passes `/live/state/` through (`tests/js/sw_routes.test.mjs`).
- Choices and the path to real push: `docs/decisions/kan-148-shared-scoring.md`.

## Shared scoring starts from a lobby (KAN-151 / KAN-152)

`live_lobby_views.py`; flag `shared_scoring` gates only the entry points (the
match form button, `LiveLobbyCreateView` 404s, the Today card), never an open
lobby or a join. The join link's credential rules are in `clubs.md`; the
rollout runbook is in `docs/decisions/kan-148-shared-scoring.md`.

- **`LiveLobby` expiry is lazy** (`LOBBY_TTL` 30 min): `current_status` reads
  an open lobby past `expires_at` as expired; no job flips it. One open lobby
  per host (`open_lobby` closes the previous; a partial unique index backs it).
- **`services.start_shared_match(lobby, joiner, request)` is the only join**:
  conditional `.update()` claim, then `create_live_match(..., can_score_both=
  False)` with `club=lobby.club`, then the guest row (side 2). Both phones
  score only their own side once KAN-150 enforces it.
- **The lobby page works with scripting off**: server SVG QR (`qr_svg`),
  selectable URL, "Check again" reload, and the view redirects to the
  scoreboard once joined. `live_lobby.js` (deferred, `json_script` config)
  polls the status every 2 s while visible and reveals Share/Copy only where
  supported; they start `hidden`, held by `[data-live-lobby] [hidden]` in
  app.css. Lobby, status and join pages are `never_cache` (no-store).
- **Today lists clubmates' open lobbies** (KAN-157, `lobbies_waiting_for`):
  open, unexpired, `open_to_club`, active club, not the viewer's, newest 3.
  Queried on every load (no cache to invalidate), never amber. Choices:
  `docs/decisions/kan-151-152-live-lobby.md`.
- e2e: the create/cancel POSTs redirect, so `applyProdCSP`'s `route.fetch`
  keeps the old URL; `releaseProdCSP` first or navigate to the lobby URL.

## The batch sync endpoint (`live_sync`, KAN-88)

`POST matches/<pk>/live/sync/` with `{"ops": [{"seq", "type", "side" |
"initial_server", "client_ts"?, "expects"?}, ...]}`; the scoreboard gets the URL as
`data-sync-url`.

- **Validate the whole batch first** (400; more than `SYNC_MAX_OPS` = 500 is
  413): a malformed op is a client bug and must not half-apply a batch.
- One `transaction.atomic()` + one `select_for_update()`; ops in `seq` order
  through `live_ops.apply_op`, so the per-point savepoint stays **per op**.
- Duplicates are skipped. A gap is 409 `gap` if nothing new was applied,
  else 200 with `rejected: {"seq", "error": "gap"}`.
- **Partial success is explicit**: the first rule rejection stops the batch,
  is named in `rejected`, and never rolls back the ops before it.
- After the completing op, the next is rejected `"match complete"`;
  completion and rollups therefore happen at most once. `redirect_url` is
  match_confirm whenever the match has finished.
- Response: canonical payload + `applied_seq` + `rejected` (null when none).
  Query cost is linear in the batch (`test_live_sync.py` pins a ceiling).
- A point's `client_ts` becomes `PointEvent.client_recorded_at`, clamped to
  `[match day - 1 day, now]`, null when unparseable; never a reason to refuse
  the point. `created_at` stays server time; prefer `client_recorded_at` for
  timing features. Choices: `docs/decisions/kan-77-78-live-sync.md`.

## `point_stats.py` is a pure module

No Django, no DB; `tests/test_point_stats.py` has **no `django_db` marker** and
that absence is the assertion.

- It takes plain `Point` tuples, never model instances.
- **Every rate carries its denominator** (`Rate(won, total)`). `Rate()` with no
  sample is `0.0`, never `None` or a `ZeroDivisionError`.
- `DEUCE_THRESHOLD` derives from `WIN_POINTS`. `COMEBACK_DEFICIT` is 3.
- **Point-level figures include an unfinished game; game-level figures don't.**
- **`combine_side_stats()` sums numerators and denominators, never averages
  rates.** Single bests (longest run, largest deficit) take the maximum.

## Rollups are a cache; the raw log is the record

`MatchPointSummary` is one row per (match, player), written at completion and
rebuildable from `PointEvent` with `manage.py rebuild_point_rollups`.
`point_rollups.py` is the Django layer above `point_stats.py`.

- **Per player, not per side.** In doubles both partners get the side's figures.
- **Counts are stored, never rates.**
- **Written by `live_ops` at completion, not a `post_save` receiver**,
  to keep it out of the completion pipeline. Non-fatal on failure.
- No compare-and-set: rollups are deterministic, `update_or_create` is enough.
- **A match with no log has its rows deleted, not zeroed.**
- **`check_match` / `--check` report and never repair.**
- Cache keys are `player_point_stats_key` / `h2h_point_stats_key` in
  `cache_utils`, cleared by `invalidate_match_caches` / `invalidate_player_caches`.
  A player's key is namespaced by that player's club.

## What the point-level screens promise

- **Every rate appears with the counts it came from.**
- **A small sample is marked, never withheld** (`MIN_SAMPLE_POINTS` = 50,
  `MIN_SAMPLE_GAMES` = 5), with a word plus a glyph, never colour alone.
- **Every surface is absent, not empty, without data.** Match detail ships no
  Chart.js to a page with no chart.
- **Match detail draws one canvas per game** (x-axis is points within a game)
  and reads the raw log; player and head-to-head screens read rollups only.
- **Chart colours are the players' hues** (`player_hues.hue_hex`). Axis/grid
  colours are read from `--bone-muted` / `--court-line` with `getComputedStyle`
  and passed as literal `rgb()`; a custom-property string renders black. No amber.
- **Head-to-head restricts to shared matches at the query level.**
- **`{% if %}` goes inside `{% block extra_js %}`, never around it**; a block is
  registered whatever encloses it.
- E2E traps: on match and player lists the name is not inside the anchor, so use
  `openMomentumMatch` / `openPlayerByName` in `helpers.js`. Chart.js logs one
  `new Function()` refusal under the e2e `PROD_CSP` and draws anyway; do not add
  `'unsafe-eval'` to `PROD_CSP`.

## Scoreboard client (`scoreboard.html`, KAN-80/81)

**Local state = server state + the outbox's unsynced ops**, replayed with
`live_rules.js`. One state model; the old optimistic overlay is gone.
Choices: `docs/decisions/kan-80-81-offline-client.md`; the epic as a whole:
`docs/decisions/kan-76-offline-scoring.md`.

- **Plain JS in static files, no Alpine, nothing inline**: scripts load
  `defer` in order `live_rules.js`, `live_outbox.js`, `live_session.js`,
  `scoreboard.js`, after the json_script data (`scoreboard-bootstrap`,
  `scoreboard-config` with `match_pk`/`user_id`/`login_url`/`longpoll_abort_ms`,
  `live-rules-config`). It runs under the e2e `PROD_CSP`; keep it that way.
- **Every action is an op persisted before anything is sent**
  (`LiveOutbox`, localStorage, keyed by match pk AND user id). Undo of an
  unsynced point is just another op; never cancel queued ops.
- **`LiveSession` owns the flush loop**: single flight, 150 ms debounce,
  `online`/visible nudges, backoff 1/2/5/10/30 s + jitter, 8 s abort.
  Reconcile drops ops <= `applied_seq`; server state != prediction means the
  server wins (toast + report). `LiveStateView` only overwrites local state
  when the outbox is empty.
- **The base is the newer of the bootstrap and the outbox's stored snapshot**,
  so a page the service worker serves stale still rebuilds correctly.
- **Never disable a tap zone** (no `disabled`, ever); only the side-switch
  prompt, the finished match and follow mode ignore taps.
- **Completion is shown locally, navigated only on the server's
  `redirect_url`**; the finish screen works offline indefinitely.
- **A 403 is first retried once with a fresh CSRF token**: a page the worker
  served from cache can hold a token from an older secret, so `/live/state/`
  returns `csrf_token` (`get_token`) and `fetchTransport` keeps it. Only a
  403 with the current token, or a login bounce, means signed out: that
  keeps the outbox and stops the loop until the user
  signs in (the login view honours a safe `next`). **Follow mode** (read-only)
  is for the same user on another device: my stream's `applied_seq` past
  anything this outbox issued, or (single-scorer only) a gap that does not
  resolve.
- **Overlays start `hidden`** and only `scoreboard.js` reveals them; the inline
  `#scoreboard [hidden]` rule beats Tailwind's display utilities. Classes
  toggled by JS don't exist: Tailwind never scans JS (pips use `data-won`).
- The sync indicator is a word plus a glyph and never amber; the leave guard
  is `beforeunload` plus an in-page sheet. **No `alert()`/`confirm()`.**
- Reports go to `console.warn`, `window.ttstatsReport` and a `ttstats:report`
  event (no client Sentry yet).
- Haptics live in the `HAPTICS` table and fire in the tap handler. Keep them few.
- Tests: `tests/js/live_outbox.test.mjs`, `live_session.test.mjs` (fake
  live_sync in `live_fakes.mjs`); e2e `scoreboard.spec.js` and the chaos cases
  in `scoreboard-offline-cases.js` (WebKit + desktop Chrome, plus
  dark/reduced/nojs). e2e traps: `releaseProdCSP` before a redirecting
  navigation; `tap()` not `click()` for rapid taps in WebKit.

### Shared mode (KAN-153): `scoreboard-config.shared`

`shared` (`match.is_shared`) and `me` come from the bootstrap. With `shared`
false nothing below runs and the page sends no long-poll (e2e asserts it).

- **Layout is server-rendered**: `data-shared` + `data-my-side` on
  `#scoreboard` for a phone without `can_score_both`; the inline CSS stacks
  the opponent's read-only zone (a `<div>`: no button role, no tabindex, no
  `.tap-zone`) above my "+1 {name}" button. One template, one sides loop.
- **Rebase, not follow**: a base is newer by `(rev, applied_seq)`; my pending
  ops replay on any newer base, silently. Divergence ("server wins") is judged
  only when no other stream moved (sum of the other `scorers`' `applied_seq`).
  A gap that does not resolve renumbers my pending ops (`renumberAfter`).
- **Undo carries `expects`**, fixed at the tap: the trailing sequence this
  phone shows (base `last_point` +1 per pending point, -1 per pending undo),
  my own queued point included. A pending undo whose `expects` no longer
  matches the replay is skipped locally; the server answers `stale undo`
  (toast, no haptic). Nothing to undo: not queued.
- **The long-poll loop** (`LiveSession`, `transport.poll`) is independent of
  the flush: its own request and AbortController (`longpoll_abort_ms`), one
  in flight, `wait=0` on load / visible / `online`, then `wait=1` re-issued at
  once; `busy` or a too-fast 204 waits `busyMs` (1 s); errors use the flush
  backoff. Stopped while hidden (`pausePolling`) and at completion.
- `ALREADY_STARTED` rebases without a toast. Queued points the new base makes
  impossible stay queued, shown as "{n} points waiting to sync". A finished
  match with my ops queued sends them; the server decides.
- The opponent's digits pulse (`.score-pulse`, outside `@layer`, off under
  reduced motion); no haptics outside the tap handler.

### Presence, takeover, removal (KAN-154)

- **Presence thresholds are settings** (`LIVE_PRESENCE_IN_SYNC` 10 s,
  `LIVE_PRESENCE_LOST` 120 s) sent as `scoreboard-config.presence`; never in
  JS. The words come from `LiveSession.presenceLine` (unit-tested): age =
  `server_now - last_seen_at` + time since the answer arrived (no clock
  skew); `polling` means in sync. IN_SYNC must stay above `SEEN_EVERY` (5 s).
- A held poll that times out (204) is followed by one plain read (no
  `since`) so presence is never older than one hold. Word plus glyph, in the
  other side's zone, **never amber**.
- `take_over` / `release` are outbox ops; `view.canScoreBoth` applies my
  pending ones, so the layout switches at the tap, offline too.
  `scoreboard.js` `applyLayout` swaps the opponent's `<div>` for a
  `<button>` (and back) keeping the template's classes and attributes.
- The board loses no height to this: "Score for both" gets a bar only while
  the other phone is lost; release and remove live in the Phones sheet
  (chrome button). Remove is online only, behind the `data-remove-sheet`
  in-page confirm.
  `removed` (403 body) stops every loop and keeps the outbox; the
  CSRF-retry-once never retries it, and it is never read as signed out.
- Today lists the live match for **every non-revoked scorer row** (row-less:
  the scorekeeper), the score read from the viewer's side.
- Tests: `live_session_shared.test.mjs` (two sessions, one fake clock);
  `live_contract.test.mjs` replays `fixtures/live_contract.json`, generated
  from the real endpoints by `pingpong.tests.live_contract`
  (`test_live_contract.py` fails when stale); e2e `shared-scoreboard*.spec.js`
  (the seeded `E2E Partner` match).
- **Two-phone e2e** (KAN-155): `shared-scoring-two-phones.js`, run by
  `shared-scoring.spec.js` (WebKit) and `.desktop.spec.js`: two contexts
  (host `user.json`, guest `e2e-partner`, `partner.json`), each test opens
  its own lobby and finishes its match. A new context needs an explicit
  empty `storageState` to be anonymous (the runner injects the project's).
  Offline via `offline-proxy.js`; the guest's match_confirm is stubbed (a GET
  confirms) so these matches stay unconfirmed. `settings/e2e.py` shortens
  the hold (6 s) and presence (6 / 20 s), env-tunable, and turns rate limits
  off (repeated join and login); the limits are pytest's.

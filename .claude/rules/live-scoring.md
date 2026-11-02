---
paths:
  - "ttstats/pingpong/live_scoring.py"
  - "ttstats/pingpong/point_stats.py"
  - "ttstats/pingpong/point_rollups.py"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/management/commands/rebuild_point_rollups.py"
  - "ttstats/pingpong/management/commands/point_storage.py"
  - "ttstats/pingpong/templates/pingpong/scoreboard.html"
  - "ttstats/pingpong/templates/pingpong/match_detail.html"
  - "ttstats/pingpong/templates/pingpong/player_detail.html"
  - "ttstats/pingpong/templates/pingpong/head_to_head.html"
  - "ttstats/pingpong/templates/pingpong/game_form.html"
  - "ttstats/pingpong/tests/test_point*.py"
  - "tests/e2e/momentum*.js"
  - "tests/e2e/point-stats*.js"
  - "tests/e2e/scoreboard*.js"
---

# Live scoring and the point log

## Scoring rules

- `live_scoring.py` owns them: `WIN_POINTS` (11), `MIN_LEAD` (2), `is_game_won`,
  `is_valid_final_score`, `common_final_scores`. Pure, no Django.
- `is_valid_final_score` is stricter than "someone is ahead": 11-10 isn't over,
  and 13-5 can't happen.
- `GameForm.clean()` defers to it; the Alt+1..4 presets in `game_form.html` come
  from `common_final_scores()` via `json_script`.

## The point log is the record; `live_state` never was

`Match.live_state["events"]` is capped at `live_scoring.MAX_EVENTS`, exists only
for Undo, and is nulled at match end. `PointEvent` is the durable record: **one
row per point, written when the point is applied**, never reconstructed later.

- **`serving_side` is read from the state BEFORE `apply_point()`.** Afterwards
  `current_server()` names who serves *next*, and the error is undetectable in
  stored data. `_record_point_event` takes `state_before` explicitly.
- **Scoring wins over recording.** The write runs in its own savepoint inside
  `LivePointView`'s transaction and swallows everything. The savepoint matters
  on Postgres, where an error poisons the whole transaction.
- **Undo deletes the trailing row** -- never a compensating "undone" event that
  every aggregation would have to filter. A failed delete rolls the request
  back. Deleting the trailing row keeps `sequence` gapless.
- **Undo guards on `had_events`**: the in-memory log is capped and the persisted
  one isn't, so without it undo past the cap deletes points never undone.
- `sequence` is `MAX(sequence) + 1` under the existing `select_for_update()` lock.
- **`Side.from_live()` / `Side.to_live()` are the only translation** between
  `"team1"`/`"team2"` and `Side`.
- **`tests/test_point_fidelity.py` is the guard.** It replays generated points
  and undos through the real endpoints, alternating the starting server with
  the seed (a harness always starting with side 1 can't see an ignored
  `initial_server`), and carries tests proving it fails against broken writes.
- **No starting server means no point.** `apply_point()` raises unless the state
  is `started` **and** names `initial_server`; `LivePointView` returns 409 before
  writing. `PointEvent.serving_side` stays nullable only as a defensive fallback.
- **Historic matches have no point rows and never will.** Every consuming
  surface must be absent rather than empty.
- Retention policy (keep everything, revisit at a threshold):
  `docs/decisions/point-event-retention.md`.

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
- **Written from `LivePointView` at completion, not a `post_save` receiver**,
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

## Scoreboard client (`scoreboard.html`)

- **The score is optimistic**: `addPoint()` bumps a local overlay rendered via
  `points(side)`; the POST reconciles.
- **Never put `:disabled="busy"` on a tap zone** -- it drops points during a
  slow POST. `busy` still gates Undo/start.
- **All POSTs go through `enqueue()`** so the server applies them in tap order.
- The overlay is only cleared when `pending` hits 0.
- Errors go to the toast via `showError()`. **No `alert()`.**
- Haptics live in the `HAPTICS` table and fire in the tap handler. Keep them few.
- It is Alpine and works in production because prod allows `'unsafe-eval'`.

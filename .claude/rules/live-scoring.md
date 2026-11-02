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
  - "ttstats/pingpong/tests/test_point*.py"
  - "tests/e2e/momentum*.js"
  - "tests/e2e/point-stats*.js"
  - "tests/e2e/scoreboard*.js"
---

# Live scoring and the point log

### Scoring Rules
- `live_scoring.py` owns them: `WIN_POINTS` (11), `MIN_LEAD` (2), `is_game_won`,
  `is_valid_final_score`, `common_final_scores`. Pure functions, no Django -- test without a DB.
- `is_valid_final_score` is stricter than "someone is ahead": 11-10 has a leader but isn't over,
  and 13-5 can't happen because play stops the moment the lead is enough.
- `GameForm.clean()` defers to it. The Alt+1..4 presets in `game_form.html` come from
  `common_final_scores()` via `json_script`. **Don't re-type scorelines anywhere** -- they were
  previously written out in three places.

### The point log is the record; `live_state` never was

`Match.live_state["events"]` looks like a history and is not one: it is capped
at `live_scoring.MAX_EVENTS`, it exists only to power Undo, and it is set to
NULL the moment the match completes. `PointEvent` is the durable record --
**one row written per point, at the moment the point is applied**, never
reconstructed at match end, which the cap makes impossible.

- **`serving_side` is read from the state BEFORE `apply_point()`.** Afterwards
  `current_server()` names whoever serves *next*. This is the single easiest
  bug to ship here and it is undetectable in the stored data, so
  `_record_point_event` takes `state_before` as an explicit argument rather
  than re-deriving it.
- **Scoring wins over recording.** The write runs in its own savepoint inside
  `LivePointView`'s transaction and swallows everything: a point that reached
  the server must count even if its history row cannot be stored. The savepoint
  is load-bearing on Postgres, where an error inside an atomic block poisons
  the whole transaction and would otherwise take the point down with it.
- **Undo is the opposite, deliberately.** `LiveUndoView` **deletes** the
  trailing row -- never a compensating "undone" event, which would mean every
  aggregation query has to filter it out and the first one that forgets
  double-counts, silently. A failed delete rolls the request back and the
  scorekeeper taps again; a log that keeps a retracted point is wrong forever.
  Deleting the trailing row is also what keeps `sequence` gapless, which is
  what makes "no gaps" usable as an integrity check.
- **Undo guards on `had_events`.** `live_state`'s list is capped while the
  persisted log is not, so on a long match the in-memory log runs out first.
  Without the guard, undoing past the cap would delete earlier points that
  were never undone.
- `sequence` is `MAX(sequence) + 1` under the existing `select_for_update()`
  row lock. A counter on `live_state` would save a query and then collide,
  because `live_state` is capped and nulled at match end.
- **`Side.from_live()` / `Side.to_live()` are the only translation** between
  `live_scoring`'s Django-free `"team1"`/`"team2"` and the `Side` enum.
- **`tests/test_point_fidelity.py` is the guard, and it is not decorative.**
  It replays generated sequences of points and undos through the real
  endpoints and asserts the persisted log reproduces what `live_scoring`
  decided. It carries permanent tests proving it fails against a broken write
  path (server read after the point; `initial_server` ignored; a dropped
  row). Removing the undo deletion fails 25 of its 30 tests.
- **The starting server alternates with the seed**, so the generated matches
  are started by both sides. A harness that always starts with side 1 cannot
  see a write path that ignores `initial_server` -- every row it checks would
  be right by coincidence.
- **No starting server means no point, never a row with a null server.**
  `live_scoring.apply_point()` raises unless the state is `started` **and**
  names an `initial_server` -- checking `started` alone let a corrupted
  `started=True, initial_server=None` state through, and `current_server()`
  then handed the log a null. `LivePointView` turns that into a 409 before
  anything is written. `PointEvent.serving_side` stays nullable only as a
  defensive fallback (`served_and_won` answers `None` for it); both states are
  pinned by the `..._no_initial_server_is_rejected_unrecorded` tests.
- **Historic matches have no point rows and never will.** Every consuming
  surface must be absent rather than empty.

### Point statistics are computed by a pure module

`pingpong/point_stats.py` joins `live_scoring.py`, `match_state.py`,
`championship_scheduling.py` and `player_hues.py` as a pure decision module --
no Django imports, no DB, testable with plain pytest and no settings.
`tests/test_point_stats.py` carries **no `@pytest.mark.django_db`**, and that
absence is the assertion.

- It takes plain `Point` tuples, never model instances. That is what keeps it
  DB-free and lets S2.4 feed it raw rows or rebuilt ones through one path.
- **Every rate carries its denominator** (`Rate(won, total)`), so a caller can
  apply a minimum-sample rule without recomputing. A 62% serve rate off 13
  points is noise presented as insight, and only the caller knows how it means
  to present it. `Rate()` with no sample is `0.0`, never `None`, never a
  `ZeroDivisionError`.
- `DEUCE_THRESHOLD` is derived from `WIN_POINTS`, not typed. `COMEBACK_DEFICIT`
  is 3, because two points is an ordinary swing in a game to eleven.
- **Point-level figures include an unfinished game; game-level figures do
  not.** An abandoned game has no winner to credit.
- **`combine_side_stats()` folds by summing numerators and denominators, never
  by averaging rates** -- averaging would weight a three-point match like a
  five-game one. Records of a single best (longest run, largest deficit) take
  the maximum; summing them would invent a run nobody played. This fold is
  what makes rollups sufficient rather than merely faster.

### Rollups are a cache; the raw log is the record

`MatchPointSummary` is one row per (match, player), written when a match
completes and **rebuildable from `PointEvent` at any time** by
`manage.py rebuild_point_rollups`. Same denormalization precedent as
`Match.is_confirmed` and the score caches. `point_rollups.py` is the thin
Django-touching layer above the pure `point_stats.py`.

- **Per player, not per side.** A side needs a join through
  `MatchParticipant` to answer "this player's career", which is the query that
  matters; a player id is an indexed column filter. In doubles both partners
  get the side's figures -- the scoreboard records which *side* served, and
  recording the individual would need UI that does not exist.
- **Counts are stored, never rates.** A career figure is a fold over
  numerators and denominators; storing rates would make it an average of
  averages.
- **Written from `LivePointView` at completion, not from a `post_save`
  receiver.** That is the one moment a match's log stops changing on its own,
  and it keeps the rollup out of the completion pipeline the epic measured
  down to 39 queries. Non-fatal: a missing rollup is rebuildable, a lost match
  is not.
- **No compare-and-set, deliberately.** A rollup is a deterministic function
  of the log, so racing rebuilds write the same numbers;
  `update_or_create` against the `(match, player)` unique constraint is enough.
  `Match.result_notified_at` needs a conditional `.update()` because "first
  writer wins" is a real decision there; here there is only one right answer.
- **A match with no log has its rows deleted, not zeroed.** Absent and zero
  mean different things to every consuming surface.
- **`check_match` / `rebuild_point_rollups --check` report and never repair**,
  the same contract as the tenancy canary.
- **Cache keys live in `cache_utils`** (`player_point_stats_key`,
  `h2h_point_stats_key`) beside every other key builder, and are deleted by
  `invalidate_match_caches` / `invalidate_player_caches`, which are already
  called from the right places. A private second copy of a key name is how an
  entry survives the invalidation meant to clear it. A player's key is
  namespaced by **that player's** club, not the viewer's.
- Club scoping is **transitive**: a `Player` or `Match` reaching these
  functions already came through a club-scoped manager and a player belongs to
  one club. The *cache* is not transitive, hence the prefixes.

### What the point-level screens promise

- **Every rate appears with the counts it came from.** A number the reader can
  check beats a number they must trust, and it is what makes "71% serving"
  worth arguing about.
- **A small sample is MARKED, never withheld** (`MIN_SAMPLE_POINTS` = 50,
  `MIN_SAMPLE_GAMES` = 5). Withholding gives no feedback and reads as a bug,
  and the counts are on screen regardless. The caveat is a word plus a glyph,
  never colour alone.
- **Every surface is absent, not empty, without data.** Most matches were
  typed in as scorelines and always will be. Match detail also ships **no
  Chart.js at all** to a page with no chart on it.
- **Match detail draws one canvas per game**, because the x-axis is points
  *within* a game -- concatenating games would put a reset to 0-0 in the
  middle of a line. It reads the raw log (it needs the progression); the
  player and head-to-head screens read rollups only.
- **Chart colours are the players' own hues** (`player_hues.hue_hex`, which
  exists for this), so the lines are the two people. Axis and grid colours are
  **read from the resolved `--bone-muted` / `--court-line` tokens** with
  `getComputedStyle` and handed over as literal `rgb()` -- passing the custom
  property *string* is the thing that renders black. No amber: amber means
  live right now.
- **Head-to-head restricts to shared matches at the query level.** Folding a
  career and subtracting would leak a player's global rate into the one
  comparison that exists to deny it.
- **A `{% block %}` in a child template is registered whatever encloses it**,
  so `{% if %}` goes *inside* `{% block extra_js %}`, never around it.
- Two spec traps, both already paid for: on the match and player lists the
  row's name is **not inside the anchor**, so `getByRole('link', {name})`
  matches a hidden node and `.tap()` times out at 30s looking like a hang --
  use `openMomentumMatch` / `openPlayerByName` in `helpers.js`. And Chart.js
  compiles part of its scale code with `new Function()`, so under the e2e
  `PROD_CSP` (no `'unsafe-eval'`, on purpose) it logs one refusal and **draws
  anyway**; production's own policy allows eval, so adding it to `PROD_CSP`
  would be the regression.

### Live Scoreboard client (`scoreboard.html`)
- **The score is optimistic.** `addPoint()` bumps a local overlay and renders through
  `points(side)`; the POST reconciles. This is not a JS copy of the rules — the server always
  increments that side first, so the displayed number is never wrong, only transient when the
  point also ends the game.
- **Never put `:disabled="busy"` back on a tap zone.** That drops points during a slow POST, which
  is the whole failure this removed. `busy` still gates the Undo/start controls.
- Because zones accept taps while a request is in flight, **all POSTs go through `enqueue()`** so
  the server applies them in tap order. Adding a new endpoint call? Enqueue it.
- The optimistic overlay is only cleared when `pending` hits 0. Clearing it on every response drags
  the display backwards while taps are still outstanding.
- Errors go to the `x-show="error"` toast via `showError()`. **No `alert()`** — it froze the page
  mid-match and read as a browser failure.
- Haptics live in the `HAPTICS` table and fire in the tap handler, on the same frame as the visual.
  Keep them few: a buzz on everything trains the umpire to ignore all of them.

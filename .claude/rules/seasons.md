---
paths:
  - "ttstats/pingpong/seasons.py"
  - "ttstats/pingpong/season_rollover.py"
  - "ttstats/pingpong/season_views.py"
  - "ttstats/pingpong/templates/pingpong/seasons/**"
  - "ttstats/pingpong/rating_pipeline.py"
  - "ttstats/pingpong/rating_replay.py"
  - "ttstats/pingpong/glicko2.py"
  - "ttstats/pingpong/glicko_adapter.py"
  - "ttstats/pingpong/ratings.py"
  - "ttstats/pingpong/management/commands/recalculate_ratings.py"
  - "ttstats/pingpong/migrations/0046_retire_elo.py"
  - "ttstats/pingpong/tests/test_glicko_pipeline.py"
  - "ttstats/pingpong/tests/test_elo_retired.py"
  - "docs/decisions/kan-73-retire-elo.md"
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/admin.py"
  - "ttstats/pingpong/migrations/0038_season.py"
  - "ttstats/pingpong/migrations/0039_player_rating.py"
  - "ttstats/pingpong/migrations/0040_backfill_player_ratings.py"
  - "ttstats/pingpong/migrations/0043_season_standing.py"
  - "ttstats/pingpong/migrations/0044_playerrating_glicko_shadow.py"
  - "ttstats/pingpong/templates/pingpong/_leaderboard_season_results.html"
  - "ttstats/pingpong/tests/test_season*.py"
  - "ttstats/pingpong/tests/test_ratings.py"
  - "ttstats/pingpong/tests/test_glicko_shadow.py"
  - "ttstats/pingpong/migrations/0045_ratingchange_glicko_history.py"
  - "ttstats/pingpong/tests/test_glicko_read.py"
  - "ttstats/pingpong/tests/test_glicko_ui.py"
  - "ttstats/pingpong/templatetags/rating_tags.py"
  - "ttstats/pingpong/templates/pingpong/how_ratings_work.html"
  - "docs/glicko2-cutover.md"
  - "ttstats/pingpong/tests/test_glicko_parameters.py"
  - "ttstats/pingpong/tests/test_glicko_dry_run_diff.py"
  - "ttstats/pingpong/tests/test_rating_backfill.py"
---

# Seasons (KAN-9)

The contract is `docs/decisions/kan-9-seasons.md`; these are the rules it implies.

- **`seasons.py` is pure** (no Django): `resolve_season`, `next_season_bounds`,
  `first_season_bounds`, `carry_over_seed_float`, `ranked`/`competition_ranks`,
  `needs_successor`, `is_due_for_rollover`, `season_name_for`. Models and the rating pipeline call it;
  never re-derive a boundary or the seed inline.
- **Dates are inclusive UTC dates.** A match's season is the one containing
  `date_played.date()`. A day in no season resolves to `None` and the match
  counts toward all-time ratings only.
- **Non-overlap is a database constraint** (PostgreSQL exclusion constraint on
  `btree_gist`; SQLite triggers). Application checks in `clean()` are for nice
  errors only. **A SQLite table rebuild drops triggers**: a migration that alters
  `pingpong_season` must recreate them.
- **Archived seasons are frozen**: club and dates read-only in the admin and
  rejected by `clean()`.
- **Season ratings are written at confirmation only, never back-filled.** A
  season created (or widened) over already-confirmed matches leaves them
  with all-time ratings only; `SeasonAdmin.save_model` warns with the exact
  `recalculate_ratings --season <id>` command that rates them.
- `Season.objects` is club-scoped (`ClubDataManager`, no user filter).
  `Season.for_day(club_id, day)` reads `all_objects` because write paths must
  resolve the *match's* club, not the viewer's.
- Seasons are always on (the `seasons` flag was retired by KAN-73).

## Ratings: Glicko-2, one system (KAN-63, KAN-73)

Elo was retired by KAN-73 (`docs/decisions/kan-73-retire-elo.md`). There is no
rating-system switch and no `seasons`/`glicko2` flag.

- **`PlayerRating` is the only rating store**: per format, all-time
  (`season=NULL`) and per season; `glicko_rating`, `rd`, `volatility`,
  `last_rated_on`, `matches`. NULL Glicko state = **unrated**. There is no
  stored peak: it is `Max(RatingChange.glicko_new_rating)` of the row.
- **`RatingChange` is the audit trail**: `glicko_old_rating`,
  `glicko_new_rating`, `glicko_change`, `glicko_rd_before` (the inflated
  pre-match RD), `glicko_rd_after`, NULL when the projection was skipped. Its
  `elo_*` columns and `EloHistory` are **frozen** pre-KAN-73 history: never
  written, never displayed, and a rebuild carries `elo_*` across unchanged.
- **`glicko_adapter.project` is the only arithmetic**; policy (one game per
  period, idle RD growth, season seeding, doubles composite opponent) lives
  there and `glicko2.py` stays pure. `DEFAULT_PARAMS` (tau 0.5, period 7
  days, season RD floor 100) are fitted (KAN-68); change them only in that
  one named tuple. Always pass `rd_max=params.rd_max` explicitly.
- **`rating_pipeline.guarded_projection` is the one path to it** for the write
  and the preview (preview == written): errors, non-finite results and RD<=0
  give None (logged), stored as NULL history, shown as 0.
- **`apply_format_scopes` is shared by the live write (`LiveBook`) and the
  replay (`rating_replay.MemoryBook`)**; never fork it. Every player gets a row
  in every scope and `matches += 1` even when the projection is skipped.
  Writing costs 3 bulk queries; keep `test_match_save_performance.py` flat.
- **`apply_match_ratings` writes every scope in one atomic block.** The guard
  is "a `RatingChange` (or a frozen `EloHistory`) exists for the match", so an
  Elo-era match without rating rows is never rated twice. A lost race hits the unique
  `(match, rating)`, rolls back and is "already applied"; any other conflict
  is retried once. Player rows are locked first so two matches sharing a
  player serialise even before either has a rating row.
- A season row is seeded from the all-time state *before* this match
  (`seasons.carry_over_seed_float`, RD inflated with a floor); a season row
  with NULL state is seeded like a missing one.
- **Doubles are rated per player against a composite opponent**
  (`composite_opponent`: mean rating, RMS RD), each with their own RD. A player
  on both sides (or twice on one) is logged and skipped.
- **`recalculate_ratings` rebuilds through `rating_replay`** in one transaction,
  holding `select_for_update` on every Player row: not safe during live
  confirmations. `--season`/`--format` rewrite only those rows. `--dry-run`
  diffs stored vs replayed `matches` and Glicko state (floats 1e-6) per player
  and scope and writes nothing; diffs of players in a match confirmed out of
  date order are listed as explained, any unexplained diff is a bug.
- **Rebuilt `RatingChange` rows are dated by their match** (`created_at` =
  `date_played`): weekly movement and the chart read `created_at`.
- **Migration 0040 is a frozen no-op**: the replay it ran no longer fits the
  0039 schema. Never import live app code into a migration again.

## The read path (`ratings.py`)

- **Every surface that shows a rating goes through `ratings.py`**: leaderboard,
  profile, nav, previews, win chances, chart. Nothing else reads `PlayerRating`
  for display. `season_rollover` reads it to freeze standings.
- **Views emit neutral keys** (`rating`, `rd`, `peak`, `status`, `reason`,
  `side1_delta`, `delta_is_pending`, `nav_rating`, `nav_rd`, `rating_chart_*`).
  `rating_system` (always `ratings.GLICKO`) comes from the context processor
  and is stamped as `data-rating-system`; the archive renders each frozen
  standing by its own `rating_system`.
- **Display RD is inflated to today** (`ratings.display_rd`, the only reader of
  "now"); caches store `rd`, `volatility`, `last_rated_on`, never a verdict.
  Provisional = display RD above `glicko_adapter.PROVISIONAL_RD`, reason
  `new` (< `ratings.NEW_PLAYER_MATCHES` all-time matches in the format) or
  `returning`. Never type either threshold in a template.
- A confirmed match shows its stored `RatingChange` (NULL = 0, never a
  re-projection). `win_probability` returns `(None, None)` on a `Glicko2Error`
  (bar hidden) and uses stored pre-match values when the match is rated.
- **Day one**: the default leaderboard scope falls back to all-time while the
  season has fewer than 2 rated players (`fell_back_to_all_time`).
- **The nav rank is counted in the PLAYER's club** (`ratings._player_club_rows`).
- **Achievements read the all-time `RatingChange` Glicko columns**: Giant
  Slayer on the pre-match gap, Peak Performer on a new max of
  `glicko_new_rating` (never below 1500).
- Pushes read the match's own season and format (`ratings.display_scope`).
  `season_rollover` freezes with the RD inflated to `season.ends_on` (not the
  run day), so a cron backlog never changes a frozen ±.
- **Rating components**: the ± is text (`_rating_value.html`: visible `±n`
  plus sr-only "give or take n"), never colour alone. Provisional/Returning is
  a bordered `label-cap`, never amber. `tests/test_glicko_ui.py` sweeps every
  surface for leftover "Elo" copy.

## The seasons UI (S4.3 / S4.4)

- **Unrated is not 1500.** No row in a scope means no number; render the
  `_rating_status.html` marker, never the seed.
- **Provisional is decided in `ratings.rating_status()`** at call time. The
  leaderboard caches `experience` and the stored RD, not the verdict, so a
  cache hit follows the thresholds too.
- **The preview scope is `ratings.display_scope(match)`**: the match's own
  season (else all-time) and format, which is exactly what confirmation
  writes. Match detail resolves it once in `get_context_data` and passes it on.
- The leaderboard's season choices are the **active club's**, filtered
  explicitly (`Season.objects` is unscoped with `clubs` off); a foreign or
  garbage id falls back to the default through `LeaderboardScopeForm`.
- The seasons table aggregates in the DB (rating rows + one grouped
  `RatingChange` query for wins and weekly movement); its query count is
  pinned flat in `test_ratings.py`.

## Rollover and the archive (KAN-51)

- **`SeasonStanding` is frozen at rollover and never written again** (only
  `notified_at`). Late confirmations move `PlayerRating`, not the table; a test
  asserts the table survives `recalculate_ratings`. Never recompute an archived
  table from ratings or matches.
- `season_rollover.run` (job `season_rollover`) has two steps:
  1. **The successor, ahead of time.** For each club whose latest season (by
     `ends_on`) satisfies `seasons.needs_successor` (`today >= ends_on -
     SUCCESSOR_LEAD_DAYS`, 14), create the next contiguous season unless one
     already starts after it. One transaction per creation, under a lock on
     the latest season; loops so a backlog catches up. Creating it at
     archive time instead would leave the new season's first days in no
     season, and those matches would never get season ratings.
  2. **The archive**, after `ROLLOVER_GRACE_DAYS`: per season, one
     transaction (freeze, `archived_at`).
- Notifications happen after the commit, claimed per (season, player) by a
  conditional `.update()` of `notified_at`: **at most once** by state (a
  crash between claim and send loses that message), never by the limiter.
- The archive views (`season_views.py`) read only `SeasonStanding`, are
  uncached and stay out of leaderboard invalidation; scoped through `Season.objects` / `SeasonStanding.objects`.
- Test data: `SeasonFactory`, `PlayerRatingFactory` and `SeasonStandingFactory`
  in `conftest.py`; never a raw `.create()` outside constraint tests.
- Render names from `player_name` via `{% player_link row.player label=row.player_name %}`:
  the player may be deleted (`SET_NULL`) or renamed.

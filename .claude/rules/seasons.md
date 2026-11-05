---
paths:
  - "ttstats/pingpong/seasons.py"
  - "ttstats/pingpong/season_rollover.py"
  - "ttstats/pingpong/season_views.py"
  - "ttstats/pingpong/templates/pingpong/seasons/**"
  - "ttstats/pingpong/elo.py"
  - "ttstats/pingpong/glicko2.py"
  - "ttstats/pingpong/glicko_adapter.py"
  - "ttstats/pingpong/elo_*.py"
  - "ttstats/pingpong/ratings.py"
  - "ttstats/pingpong/rating_divergence.py"
  - "ttstats/pingpong/management/commands/rating_divergence.py"
  - "ttstats/pingpong/tests/test_rating_divergence.py"
  - "ttstats/pingpong/management/commands/recalculate_elo.py"
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
  - "ttstats/pingpong/tests/test_rating_backfill.py"
---

# Seasons (KAN-9)

The contract is `docs/decisions/kan-9-seasons.md`; these are the rules it implies.

- **`seasons.py` is pure** (no Django): `resolve_season`, `next_season_bounds`,
  `first_season_bounds`, `carry_over_seed`, `ranked`/`competition_ranks`,
  `needs_successor`, `is_due_for_rollover`, `season_name_for`. Models and the Elo pipeline call it;
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
  `recalculate_elo --season <id>` command that rates them.
- `Season.objects` is club-scoped (`ClubDataManager`, no user filter).
  `Season.for_day(club_id, day)` reads `all_objects` because write paths must
  resolve the *match's* club, not the viewer's.
- The `seasons` flag (default off) gates every UI that reads seasons. With it
  off, pages render exactly as before.

## Ratings: the PlayerRating mirror

- **`Player.elo_rating`/`elo_peak`/`matches_for_elo` stay lifetime and
  combined**, written by the unchanged legacy arithmetic. Formats and seasons
  live only in `PlayerRating` (`season=NULL` = all-time) with `RatingChange`
  as its audit trail. `EloHistory` is untouched.
- **UI reads go through `ratings.py`** (S4.3): the leaderboard, profile and
  previews, only when the `seasons` flag is on. Nothing else reads
  `PlayerRating` for display.
- `season_rollover` also reads season `PlayerRating` rows, to freeze
  them into `SeasonStanding`.
- **`projected_elo_changes(match, scope)` is the only arithmetic** for every
  scope (`LEGACY` or `RatingScope(season, format)`). Format experience is the
  all-time count in that format. A season row is seeded with
  `carry_over_seed` from the all-time rating read *before* this match.
- **`update_player_elo` writes every scope in one atomic block.** A lost race
  hits a unique constraint, rolls back, and is "already applied" if
  `EloHistory` exists for the match; any other conflict is retried once.
- `apply_format_scopes` is shared by the live write (`LiveBook`) and the replay
  (`MemoryBook`); never fork it. Writing a scope costs 3 bulk queries; keep
  `test_match_save_performance.py` flat.
- **`recalculate_elo` rebuilds through `elo_replay`** in one transaction,
  holding `select_for_update` on every Player row: not safe during live
  confirmations. `--season`/`--format` rewrite only those PlayerRating rows;
  legacy fields and `EloHistory` are rewritten only by a full run. `--dry-run`
  diffs stored vs replayed per player and scope and writes nothing.

## Glicko-2 shadow (KAN-67)

- **`glicko_rating`, `rd`, `volatility`, `last_rated_on` on `PlayerRating` are
  an inert shadow: written, never read** (a test greps for readers). They
  replace Elo only at the cutover (`docs/decisions/kan-63-glicko2.md`).
- **One book, one traversal.** The shadow rides `apply_format_scopes`
  (`glicko=params`) on the same rows, so it adds no queries; the live path
  passes `glicko_adapter.DEFAULT_PARAMS`, the replay passes what it is given.
- **`glicko_adapter.projected_glicko_changes` is the only Glicko arithmetic**
  (preview == written value). Policy lives there; `glicko2.py` stays pure.
  Always pass `rd_max=params.rd_max` explicitly.
- **A Glicko failure must never abort the Elo write**: only the pure
  projection is wrapped in try/except (logged, scope skipped); non-finite
  results are rejected.
- **Doubles (KAN-70) are rated per player against a composite opponent**
  (`glicko_adapter.composite_opponent`: mean rating, RMS of the RDs), each with
  their own RD; only the doubles-format rows change. Sides may be any size. An
  empty side projects to `None`; a player on both sides (or twice on one) is
  logged and skipped. `side1_change`/`side2_change` are the mean delta of the
  side's players; `updates` is per player.
- `RatingWriter` writes the shadow only if the model has the fields, because
  migration 0040 runs it against the historical 0039 model.
- `DEFAULT_PARAMS` are PLACEHOLDERS until KAN-68 fits them.
- Only `recalculate_elo` rebuilds the shadow; `elo_parity` does not.
- **Deploying 0044 must be followed by `recalculate_elo --format singles`,
  and KAN-70 by `recalculate_elo --format doubles`**:
  existing rows have NULL shadow fields and would otherwise start from
  1500/RD 350 at their next confirmation. It rebuilds that format's rows
  including the shadow, leaves `Player.elo_*` and `EloHistory` alone, and is
  idempotent (pinned in `test_glicko_shadow.py`).
- A season row with NULL shadow fields is seeded like a missing one (decision
  3), so one failed projection costs only that match's update.
- **`manage.py rating_divergence` (KAN-66) is the read-only Elo vs Glicko-2
  harness** (`--tau`, `--period-days`, `--season-rd-floor`, `--top`, `--json`).
  It replays through `iter_replay_matches` + `Replay(glicko=...)` in memory (no
  shadow columns needed, no writes), scores pre-match predictions online
  (Brier, log-loss, favourite rate) and compares final rankings. End RD is
  inflated to the history's last match date, never now(), so output is
  deterministic. It reads the in-memory `MemoryBook` `RatingState` attributes of
  its own replay, never the stored columns; it is exempt from the no-readers
  test by repo-relative path (`ttstats/pingpong/rating_divergence.py`), not
  basename. Skipped Glicko projections and players left without Glicko state are
  counted in the report. KAN-68 uses it to fit the parameters. Glicko-2 is not
  zero-sum and a high-RD player moves more than an established opponent, so a
  uniform level shift with an unchanged ranking is expected (see the "Rating
  sum" line).

## The seasons UI (S4.3 / S4.4)

- **Unrated is not 1500.** No row in a scope means no number; render the
  `_rating_status.html` marker, never the seed.
- **Provisional is decided in `ratings.rating_status()`**, from the all-time
  format count against `elo.PROVISIONAL_MATCHES` read at call time. The
  leaderboard caches `experience`, not the verdict, so a cache hit follows the
  constant too. Never type the threshold in a template.
- **The preview scope is `ratings.display_scope(match)`**: the match's own
  season (else all-time) and format, which is exactly what confirmation
  writes. Match detail resolves it in `_viewer_context`.
- The leaderboard's season choices are the **active club's**, filtered
  explicitly (`Season.objects` is unscoped with `clubs` off); a foreign or
  garbage id falls back to the default through `LeaderboardScopeForm`.
- The seasons table aggregates in the DB (rating rows + one grouped
  `RatingChange` query for wins and weekly movement); its query count is
  pinned flat in `test_ratings.py`.

## Rollover and the archive (KAN-51)

- **`SeasonStanding` is frozen at rollover and never written again** (only
  `notified_at`). Late confirmations move `PlayerRating`, not the table; a test
  asserts the table survives `recalculate_elo`. Never recompute an archived
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
  uncached and stay out of leaderboard invalidation. 404 with the flag off;
  scoped through `Season.objects` / `SeasonStanding.objects`.
- Test data: `SeasonFactory`, `PlayerRatingFactory` and `SeasonStandingFactory`
  in `conftest.py`; never a raw `.create()` outside constraint tests.
- Render names from `player_name` via `{% player_link row.player label=row.player_name %}`:
  the player may be deleted (`SET_NULL`) or renamed.

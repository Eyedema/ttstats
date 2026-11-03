---
paths:
  - "ttstats/pingpong/seasons.py"
  - "ttstats/pingpong/elo.py"
  - "ttstats/pingpong/elo_replay.py"
  - "ttstats/pingpong/elo_parity.py"
  - "ttstats/pingpong/management/commands/recalculate_elo.py"
  - "ttstats/pingpong/migrations/004*_*.py"
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/admin.py"
  - "ttstats/pingpong/migrations/0038_season.py"
  - "ttstats/pingpong/tests/test_season*.py"
  - "ttstats/pingpong/tests/test_rating_backfill.py"
---

# Seasons (KAN-9)

The contract is `docs/decisions/kan-9-seasons.md`; these are the rules it implies.

- **`seasons.py` is pure** (no Django): `resolve_season`, `next_season_bounds`,
  `first_season_bounds`, `carry_over_seed`. Models and the Elo pipeline call it;
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
- **It is a mirror: nothing outside `elo.py`, `elo_replay.py`, `elo_parity.py`
  and `recalculate_elo` reads `PlayerRating` until S4.3.**
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

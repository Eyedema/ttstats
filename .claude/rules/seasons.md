---
paths:
  - "ttstats/pingpong/seasons.py"
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/admin.py"
  - "ttstats/pingpong/migrations/0038_season.py"
  - "ttstats/pingpong/tests/test_season*.py"
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

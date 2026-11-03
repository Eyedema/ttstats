# KAN-9: Seasons & Rating Integrity — design decisions

Status: agreed 2026-11-03. This is the contract every KAN-9 story builds against.
Jira: epic KAN-9; stories KAN-45 (Q4.8), 46 (S4.1), 47 (S4.2), 49 (S4.7),
48 (S4.3), 50 (S4.4), 51 (S4.6), tasks KAN-37 (T3.6), KAN-38 (T3.7).

## Product decisions (from the epic's open questions)

1. **Season length is set per club.** Staff create seasons with explicit
   `starts_on`/`ends_on` dates. Rollover creates the next season with the same
   length, starting the day after. The default length for a club's first
   season is 3 calendar months.
2. **Carry-over is a soft reset toward the mean.** A player's first rating in
   a season, for each format, is seeded as
   `1500 + SEASON_CARRY_FACTOR * (R - 1500)` with `SEASON_CARRY_FACTOR = 0.5`.
   `R` is the player's **all-time rating in that format** just before their
   first match of the season. A player unrated in that format starts at 1500.
   Season rows are created lazily, on a player's first rated match in that
   season and format. So rollover never touches ratings, and the rule applies
   identically to every player. The seed is read **before** this match's all-time
  update is applied in the same transaction.
3. **`Player.elo_rating`, `elo_peak` and `matches_for_elo` stay exactly as they
   are today: lifetime, singles and doubles combined.** They are written by the
   unchanged legacy arithmetic. Parity (Q4.8) therefore holds permanently, not
   only until cutover. Seasons and formats live only in `PlayerRating`.
4. **Rollout:** a new `seasons` feature flag, default off. With it off, every
   page renders exactly as today. The `PlayerRating` mirror is written whether
   or not the flag is on.

## Data model

- `Season(club FK PROTECT, name, starts_on: date, ends_on: date, archived_at: datetime|null)`.
  - Both dates are inclusive. A match belongs to a season when
    `starts_on <= date_played.date() <= ends_on`. `TIME_ZONE` is UTC, so the
    date is the UTC date. That is the timezone decision.
  - `CheckConstraint(ends_on >= starts_on)`.
  - **Non-overlap within a club is enforced in the database.** On PostgreSQL:
    an `ExclusionConstraint` using `btree_gist`
    (`club WITH =`, `daterange(starts_on, ends_on, '[]') WITH &&`). On SQLite
    (the test DB): `BEFORE INSERT`/`BEFORE UPDATE` triggers that `RAISE(ABORT)`.
    The migration branches on `schema_editor.connection.vendor`, and both
    directions reverse cleanly.
  - Once `archived_at` is set, a season's dates cannot be edited
    (`clean()` + admin read-only).
- `pingpong/seasons.py` is a **pure** module with no Django imports. It holds
  `resolve_season(day, seasons)`, which returns the containing season or None.
  It also holds `next_season_bounds(...)` and the carry-over formula. It is
  tested without the DB.
- **A match outside every season** (before the first one, or in a gap) counts
  toward all-time ratings only. That is defined behaviour, not a silent drop.
  Rollover creates contiguous seasons, so gaps only appear when staff create
  them by hand.
- `PlayerRating(player FK CASCADE, club FK, season FK null, format: 'singles'|'doubles', rating, peak, matches, updated_at)`.
  - `season=NULL` is the **all-time** rating for that format.
  - Unique constraints: `(player, season, format)` WHERE `season IS NOT NULL`,
    and `(player, format)` WHERE `season IS NULL`. Use conditional
    `UniqueConstraint`s rather than `nulls_distinct`, because tests run on
    SQLite.
  - Index `(club, season, format, -rating)` for the leaderboard.
  - `peak` is per row: per format, per season, and per format all-time.
- `RatingChange(match FK CASCADE, player FK, rating FK PlayerRating, old_rating, new_rating, rating_change, k_factor, created_at)`,
  unique `(match, rating)`. This is the audit trail for `PlayerRating`.
  `EloHistory` stays the immutable legacy audit trail. Its rows and semantics
  do not change: it still records the lifetime combined rating.

## Arithmetic: one function, several scopes

- The provisional threshold moves to a constant: `elo.PROVISIONAL_MATCHES = 20`.
  `calculate_k_factor(match, player)` keeps its signature and behaviour.
  It delegates to `k_factor(match, matches_played)`.
- **Experience (the provisional boost and the S4.4 marker) for format ratings
  is the player's all-time match count in that format.** The season count is
  not used. A new season therefore does not make an established player
  provisional again.
- `projected_elo_changes(match, scope=LEGACY)` stays the **only** arithmetic.
  `scope` selects where ratings and match counts are read from:
  - `LEGACY` reads `Player` fields (today's behaviour, byte for byte).
  - `RatingScope(season, format)` reads `PlayerRating` (the all-time format
    rating when `season` is None), with seeds per decision 2.
- `update_player_elo(match)` writes all of the following in **one**
  `transaction.atomic()`, under the existing guard:
  - the LEGACY scope to `Player` and `EloHistory` (unchanged),
  - the all-time format scope to `PlayerRating` and `RatingChange`,
  - the match's season-format scope, when the match falls in a season.

  If the guard is lost to a concurrent confirmer, nothing is written twice.
  The unique constraints make a duplicate insert roll back the whole block,
  and the losing caller treats `IntegrityError` as "already applied", without
  raising.
- With the flag on, the UI preview (dashboard "if true" and match detail) uses
  the scope that the viewer's leaderboard displays: the current season and the
  match's format. It calls the same `projected_elo_changes`. Preview and write
  remain the same call.

## Rebuild (S4.7) and parity (Q4.8)

- Deterministic replay order everywhere: `('date_played', 'created_at', 'pk')`.
  Only matches with `winner_side` set **and** `is_confirmed=True` are replayed.
  Championship matches that have a winner but are unconfirmed are out of scope.
  That is how `update_player_elo` already behaves.
- `recalculate_elo` keeps its interface and output style. It also rebuilds
  `PlayerRating` and `RatingChange`, in batches (`iterator(chunk_size=...)`),
  inside one transaction, so an interruption leaves everything fully old or
  fully new. `--dry-run` computes the rebuild in memory and prints a per-player
  diff table (stored vs. recomputed rating, peak and matches, per scope).
  `--season <id>` and `--format singles|doubles` limit the PlayerRating rebuild.
  `EloHistory` is rewritten only by a full, unscoped run, which is today's
  behaviour.
- The rebuild is **not** safe to run while confirmations are happening. The
  command documents that it takes `select_for_update` on Player rows for its
  duration. That is acceptable at this scale.
- `pingpong/elo_parity.py` (harness) is invoked by
  `manage.py elo_parity [--inject-k-factor X]` and by pytest. It replays history
  through (a) a frozen copy of today's legacy arithmetic and (b) the new
  pipeline. It asserts identical rating, peak and match counts per player, and
  identical per-match `rating_change`/`k_factor`. For singles-only players it
  also asserts that all-time singles `PlayerRating` equals the legacy value,
  **restricted to players whose connected component of the match graph
  contains no doubles match** (amended during KAN-47). Without that
  restriction the invariant is false by construction: a singles-only player
  whose opponent also plays doubles meets the opponent's *combined* legacy
  rating in one pipeline and their *singles* rating in the other. It prints a
  per-player diff table.
- Stored production values may differ from any replay, because live Elo is
  applied in confirmation order and replay uses play order. `--dry-run`
  reports this. If production shows diffs, a one-time real
  `recalculate_elo` run is the fix, and that is a decision for the user.

## Jobs and notifications (T3.6 / T3.7)

- Host cron calls one dispatcher, `manage.py run_scheduled_jobs [--job NAME] [--dry-run]`.
  The crontab is committed under `deploy/cron/`. Each job takes a cache lock
  (`cache.add`). It **fails closed** when the cache is unreachable. Under
  LocMemCache the lock is not cross-process; that is documented. Every
  non-dry run writes a `JobRun` row (name, started, finished, status, summary,
  error), including skipped ones. A `--dry-run` takes no lock and writes no
  row, so that it writes nothing at all. Jobs are registered with
  `@jobs.register(name)` in `pingpong/scheduled_jobs.py`. The first job is
  `prune_job_runs` (180-day retention). `season_rollover` registers there
  too. Not Celery, because the VPS has 3.7 GB of RAM and push.py deliberately
  runs without a broker.
- New `NotificationKind.SEASON_ENDED` (with a matching `push_season_ended`
  field, default True). A cache-based per-kind and overall per-player daily
  counter fails closed. The `notify_*` functions still return the set of
  Player ids reached, and callers email the rest.
  Defaults: `season_ended` is limited to 1 per player per UTC day, with an
  overall ceiling of 50 per day across kinds (`NOTIFICATION_DAILY_LIMITS` and
  `NOTIFICATION_DAILY_CEILING`). The limiter runs inside
  `push.send_to_user`. A push that is rate limited is "not reached", so the
  caller's email fallback applies.
  `notify_season_ended(player, *, season_name, standings, url)`
  takes `standings` as `[(format_label, rank), ...]` and sends **one** push
  per player, covering every format, so the 1/day limit never drops a
  player's second format. Rollover calls it once per player per archived
  season.

## Rollover (S4.6)

- The job `season_rollover` processes seasons where
  `ends_on + ROLLOVER_GRACE_DAYS (3) < today` and `archived_at` is null.
  It freezes `SeasonStanding(season, format, player, rank, rating, matches, wins, losses)`
  rows, sets `archived_at`, creates the next season if none exists, and
  notifies each player of their final rank. It is idempotent: an archived
  season is skipped, and the next season is created only once.
- **Late confirmation rule:** a match whose `date_played` falls in an archived
  season still updates the all-time and season `PlayerRating` rows when
  confirmed. **The frozen `SeasonStanding` rows never change.** The archive is
  the record as it stood at rollover. The grace period exists to make this
  rare.
- Ties share a rank (1, 1, 3), ordered by rating, then wins, then name for
  display.

### As built (KAN-51)

- `SeasonStanding` also stores `player_name` (the display name at freeze
  time) and `notified_at`. `player` is `SET_NULL`, so deleting a player keeps
  their row and name; a rename does not reach the archive. `season` is
  `PROTECT`. There is no club column: the manager scopes through
  `season__club`.
- `rating` and `matches` come from the season's `PlayerRating` row; wins come
  from that row's `RatingChange`s where the player was on the winning side, and
  losses are the rest. Rank is by rating alone (`seasons.ranked`).
- One transaction per season: lock the row, freeze, set `archived_at`, create
  the next season. A season that fails rolls back, is logged and skipped, and
  the run raises at the end so the `JobRun` shows the failure; other clubs
  still roll over. A backlog catches up in one run (a next season that is
  already due is archived in turn).
- The next season is created only when the club has **no season starting
  after** this one ends (so it can never hit the non-overlap constraint). Its
  default name comes from `seasons.season_name_for` ("Apr–Jun 2026"); staff
  can rename it.
- **Notifications run after the commit, never inside it.** Each (season,
  player) is claimed with a conditional `.update()` of `notified_at`, as
  `Match.result_notified_at` does; only the winner of the claim sends one push
  covering every format, then emails a verified address if push did not reach
  them. This is at most once: a crash between claim and send loses that
  message rather than risking a double. Unclaimed rows left by a crash after
  archiving are picked up by the next run.
- Rollover itself ignores the `seasons` flag (data is written either way). A
  player for whom the flag is off is claimed but not told, so turning the flag
  on later never announces an old season.
- A live match belongs to the season of its `date_played` (its start). One
  that finishes after rollover is a late confirmation like any other.
- `--dry-run` reports only the seasons due now; it does not simulate a
  backlog's follow-on seasons.
- The archive (`/pingpong/seasons/`, `/pingpong/seasons/<pk>/?format=`) reads
  only `SeasonStanding`, is not cached and is outside leaderboard
  invalidation. It is 404 with the flag off. It has no nav entry yet: a
  flag check in the drawer costs a query on every page (it broke the
  match-list query budgets), so the link belongs to the S4.3 leaderboard.

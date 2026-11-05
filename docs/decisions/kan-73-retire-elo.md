# KAN-73: retire Elo

Status: implemented 2026-11-05, the same day Glicko-2 went live for everyone.
Epic: KAN-63. Contract for the rating system itself: `kan-63-glicko2.md`.

## Timing

The ticket and `docs/glicko2-cutover.md` said not to do this before the end of
the first Glicko season (2027-01-31): stopping the Elo writes is what makes
the cutover irreversible by flags. The owner chose to do it on cutover day
and accepted that. After this change the only rollback is a restore of the
pre-KAN-73 dump, which also discards every match played since.

## Decisions

1. **One system, no switch.** The Elo arithmetic (`elo.py`), the frozen Elo
   reference (`elo_parity.py`), the Elo-vs-Glicko harness
   (`rating_divergence.py`) and the Elo display path are deleted. No dormant
   copy behind a flag. The divergence harness's findings are recorded in
   `kan-63-glicko2.md` ("Parameters (KAN-68)").
2. **Both flags retired.** `glicko2` was T6.10's flag. `seasons` goes too: its
   off position rendered the pre-season Elo screens (leaderboard by
   `Player.elo_rating`, nav "1512 Elo"), which can no longer exist. Overrides
   of either are deleted by migration 0046; a stale name in
   `TTSTATS_FLAGS_ON` is ignored.
3. **`Player.elo_rating` / `elo_peak` / `matches_for_elo` are dropped**
   (Open Question 3, decided explicitly). They were a derived "current Elo",
   recomputable from `EloHistory`. Kept, they would go stale on the first
   confirmation and keep teaching the wrong model to every reader of the
   model, the admin and fixtures.
4. **`PlayerRating.rating` / `peak` are dropped** for the same reason. The
   Glicko peak is `Max(RatingChange.glicko_new_rating)` of the row, as the UI
   already computed it.
5. **History is frozen, not deleted** (Open Question 6, as already decided in
   `kan-63-cutover-plan.md`). `EloHistory` is untouched and read-only in the
   admin. `RatingChange`'s Elo columns are renamed `elo_*`, made nullable,
   never written again, and carried across a `recalculate_ratings` rebuild
   unchanged. No historical row is rewritten.
6. **Achievements move to Glicko.** Giant Slayer compares pre-match all-time
   Glicko ratings (`glicko_old_rating`); Peak Performer is a new all-time max of
   `glicko_new_rating` with a positive change, floor 1500. The thresholds
   (100/200/300) are unchanged: Glicko-2 is on the Elo scale. Already-earned
   achievements are kept. The descriptions no longer say "Elo".
7. **The idempotency guard is "a `RatingChange` or an `EloHistory` row exists
   for the match"**. Every rated match has `RatingChange` rows (the KAN-47
   backfill gave them to the whole history); the frozen `EloHistory` check is
   a safety net so an Elo-era match that somehow has no rating rows is never
   rated a second time on top of today's ratings. The unique
   `(match, rating)` constraint still turns a concurrent second confirmation
   into "already applied".
8. **`recalculate_elo` becomes `recalculate_ratings`**, same flags. It never
   touches `EloHistory`. The dry run compares `matches` and the Glicko state.
9. **Migration 0040 is a frozen no-op.** It ran the live replay against the
   0039 models; the replay no longer fits that schema. Prod and the Supabase
   clone applied it before KAN-73, and a fresh database has nothing to
   backfill. A database with history first migrated past 0040 after KAN-73
   must run `recalculate_ratings` right after `migrate`.
10. **Cache keys keep the `g2_` infix**, so an Elo-shaped entry left in Redis
    from before the deploy (an int nav rank) can never be read.

## Deploy

See "KAN-73: retire Elo" in `docs/glicko2-cutover.md`.

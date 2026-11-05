# KAN-63: Replace Elo with Glicko-2 — design decisions

Status: S6.1 (KAN-64) agreed with the product owner 2026-11-05. This is the
contract every KAN-63 story builds against. Later stories append their
decisions (τ, seeding, doubles, cutover) here.

Jira: epic KAN-63; stories KAN-64 (S6.1), 65 (S6.2), 66 (Q6.3), 67 (S6.4),
68 (S6.5), 69 (S6.6), 70 (S6.7), 71 (S6.8), 72 (T6.10), 73 (S6.9).

## The data behind the decision (KAN-64)

The data comes from the Supabase clone of prod, taken 2026-11-03: 101 confirmed matches,
2025-03-31 → 2026-05-29, 3 players, one club, 0 doubles.

| player | matches | active days | matches per active day (min / median / max) | longest idle gap |
|---|---|---|---|---|
| 1 | 87 | 11 | 2 / 4 / 45 | 265 days |
| 2 | 101 | 13 | 2 / 4 / 45 | 265 days |
| 3 | 14 | 2 | 5 / 9 / 9 | 332 days |

Weekly windows look the same as daily ones (median 4 per active week), and so
do monthly ones (median 8). The median gap between consecutive matches is 0
days. **Play comes in sessions.** There are several matches on one evening,
then weeks or months of nothing. A weekly or monthly batch would almost always
contain exactly one session. Batching would therefore cost the exact-preview
promise and per-match idempotency, and gain almost none of the
"rate co-occurring games together" benefit. What the data does need is RD
growth over the long idle gaps.

## Decisions

1. **Period model: one match per rating period, with RD inflated by elapsed
   time.** Each confirmed match is a Glicko-2 rating period containing one
   game. Before the update, each participant's φ is inflated for the idle time
   since their last rated match in that scope:

       t  = max(0, days(match.date_played.date() − last_rated_on)) / RATING_PERIOD_DAYS
       φ* = min(√(φ² + t·σ²), φ_max)

   - Matches on the same day have t = 0, so no inflation happens inside a
     session.
   - A confirmation out of date order gives a negative gap, which is clamped
     to 0.
   - The opponent's φ in `g(φ)` is the opponent's **inflated** φ*. That is
     their uncertainty as of this match.
   - `RATING_PERIOD_DAYS` (the time unit for σ²) and φ_max are policy
     constants. S6.5 fits them and records them here. `glicko2.py` keeps
     `MAX_RD = 350` only as Glickman's algorithm default. The adapter always
     passes the policy value explicitly, so it never relies on that default.
   - This needs `last_rated_on` on `PlayerRating` (S6.4).
   - The pure module exposes the standard rating-period update, which takes a
     list of (µ_j, φ_j, s_j) and allows zero games. It also exposes a separate
     idle-inflation function. The per-match policy lives in the Django
     adapter, so the module stays a faithful implementation of Glickman.
2. **The confirmation promise is kept exactly.** The preview shown before
   confirming and the value written at confirmation come from **one function**,
   just as `projected_elo_changes(match, scope)` does today. It is deterministic
   given the stored rating state and `match.date_played`, and it never reads
   "now". The copy "+14 if agreed" stays. The Glicko-2 projection replaces the
   Elo one at cutover (S6.8), keeping the same contract and the same race guard.
   The preview is still subject to the caveat that already exists under Elo:
   if another match involving the same player is confirmed first, the stored
   state moves and the preview is recomputed on the next render.
3. **Seasons and Glicko-2 ship as one event (answers Open Question 7).** The
   `seasons` flag stays **off** until the Glicko-2 cutover. At cutover, staff
   create the first season and both flags flip together, so players see one
   rating change, not two. A season boundary is **not** a rating period:
   - Every `PlayerRating` row (all-time and season, per format) carries its
     own r, RD, σ and `last_rated_on`.
   - A player's first season row is seeded as
     r = `carry_over_seed(all-time r)` (KAN-9, unchanged), and
     RD = max(all-time φ* inflated to the match date, `SEASON_RD_FLOOR`).
     σ is the all-time σ. A soft reset of r is a loss of information, so RD
     rises with it. The floor is fitted in S6.5.
   - Off-season RD growth then happens naturally through decision 1. The two
     answers to "what happens over a break" are consistent: r is reset by the
     season rule, and uncertainty is grown by time.
4. **The displayed number is r on the ~1500 scale, with ±RD (answers Open
   Question 5).** Ranking is by r. When RD is high, the UI shows a
   provisional marker and the ±RD band. The KAN-50 `rating_status()` signal
   switches from a match count to an RD threshold (S6.8). A bare number is
   never shown without its confidence when confidence is low.

## Consequences for downstream stories

- **S6.2:** the signature follows from decision 1. It provides
  `rate(rating, games, tau)` for a period with 0..n games, and
  `inflate(φ, σ, periods, φ_max)` for idle time. It has no notion of match or player.
- **Q6.3 / S6.6:** the replay must apply the same idle-time rule. Live
  confirmations are applied in confirmation order, while a replay is ordered by
  `(date_played, pk)`. These can legitimately disagree when a match is
  confirmed out of date order, which is the same effect that left 3 legacy
  Elo ratings off before the KAN-9 recalc. The incremental-versus-recompute check
  in S6.6 must detect such matches and report them separately. A raw diff on
  them is not counted as a pipeline bug.
- **S6.4:** `PlayerRating` gains `rd`, `volatility` (both `FloatField`) and
  `last_rated_on` (date, null).
- **S6.5:** fit τ, `RATING_PERIOD_DAYS` and `SEASON_RD_FLOOR`, and record
  them here.

## Doubles (KAN-70, answers Open Question 4)

**Each player is rated per player, in a one-game period, against a composite
opponent made from the other side.** For a player `i` on one side:

- The opponent is one virtual player with
  `r_opp = mean(inflated r of the opposing players)` and
  `RD_opp = sqrt(mean(inflated RD^2 of the opposing players))` (the RMS, not the
  plain mean of RDs). Two well-known players form a predictable pair; one RD-350
  partner raises the pair's uncertainty.
- Player `i` is then updated exactly as in singles (decision 1): their own
  inflated rating, RD and volatility against `(r_opp, RD_opp)`, with the team
  result as the score. Their own partner does not enter the update, so a new
  player (high RD) learns much more than an established partner from the same
  result. That asymmetry is the point.
- Only the doubles-format `PlayerRating` rows change (all-time, and the season
  row if any). Singles rows are untouched, and vice versa. Seeding, idle
  inflation and the season floor are the singles rules, per format.
- A side can have any number of players; nothing assumes 2. An empty side
  keeps the existing guard (no projection). A player on both sides (or twice
  on one side) is a data error: it is logged and Glicko is skipped for that
  match, while Elo is unaffected.
- Preview equals written: `projected_glicko_changes` returns one `GlickoUpdate`
  per player in `updates`; those per-player values are what the UI shows.
  `side1_change` / `side2_change` are per-side summaries: the rounded **mean
  delta of that side's players** (for singles, the player's own delta).
- The side-combination policy is the pure helper
  `glicko_adapter.composite_opponent(readings)`; `glicko2.py` is unchanged.
- `rating_divergence` reports doubles in their own section. Its pre-match
  prediction treats each side as one composite player (mean rating, RMS RD).

**Rejected: a pair as one composite entity with its own phi, or "weighted
per-player attribution".** Pairs are fluid, so a pair entity would need its own
persisted state (and a policy for pair identity, partial overlap and
dissolution). Weighted attribution needs an invented weighting. The per-player
update needs neither and keeps the stored state exactly what singles stores.

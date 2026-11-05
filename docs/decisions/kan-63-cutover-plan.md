# KAN-71 / KAN-72 — Glicko-2 read cutover: implementation plan

Status: the architect's plan was accepted on 2026-11-05. This is the working spec for the two
implementation chunks. Contract: `kan-63-glicko2.md`.

## Decisions (beyond kan-63-glicko2.md)
- **Flag `glicko2`** is added to `FEATURE_FLAGS` (settings/base.py, default False). It uses the existing
  helper (`is_enabled`, overrides, `TTSTATS_FLAGS_ON`).
  - `ratings.system(user)` returns `GLICKO` only when **both** `seasons` and `glicko2` are on.
    Otherwise it returns `ELO`.
  - With no user (jobs, `system(None)`), it uses the settings value.
  - A Django system check warns when `glicko2` is on and `seasons` is off.
  - A context processor exposes `rating_system`, so the nav and the page body cannot disagree.
  - Only three states exist: today's Elo, seasons+Elo (S4.3, already tested) and Glicko.
- **Open Question 6 is answered by freezing `EloHistory`.** Glicko history goes on `RatingChange` (migration 0045, all
  nullable): `glicko_old_rating`, `glicko_new_rating` (Float), `glicko_change` (Int, same
  rounding as the preview), `glicko_rd_before` (Float, the inflated pre-match RD) and `glicko_rd_after`
  (Float). `SeasonStanding` gains `rating_system` (Char, default "elo") and `rd` (Float, null).
  - These are written by `LiveBook.flush` and `RatingWriter.flush` from `FormatChange.glicko`, or NULL when it is missing.
  - `RatingWriter` checks that the fields exist on `change_model`, because migration 0040 runs it on the historical model.
  - `recalculate_elo --format singles|doubles` backfills them in the same pass as the shadow.
- **Doubles are rated (KAN-70 landed)**: doubles previews, deltas and win probabilities exist in
  Glicko. For win probability, each side is treated as a composite player via
  `glicko_adapter.composite_opponent`.
- **Day-one empty table**: the default leaderboard scope falls back to all-time while the current
  season has fewer than 2 rated players in that format.
- **Achievements**: the copy becomes system-neutral ("rated well above you") and the logic stays on Elo until
  KAN-73. This is recorded as a known gap.
- **Provisional (Glicko)**: the display RD is above `glicko_adapter.PROVISIONAL_RD`. The placeholder is 110 and it
  will be tuned after KAN-68 against the DISPLAY RD, i.e. inflated to today.
  - `rating_status` returns a `reason`: "new" for few matches, or "returning" when RD is high due to idle time.

## Inventory: every rating read and how it switches (S = `ratings.system(request.user)`)
| # | Surface | With Glicko |
|---|---|---|
| 1 | Season leaderboard: `ratings.leaderboard` (ratings.py ~129-196), `_season_context` / `_presentation_context` (views.py ~1517-1582) | `leaderboard(scope, top_x, system=S)`. Rows use neutral keys `rating` (int), `rd`, `peak`, `last_rated_on`. Sorted by `glicko_rating`. A NULL glicko row is unrated. Movement is `Sum("glicko_change")` and peak is `Max("glicko_new_rating")`. The query count stays flat. |
| 2 | Legacy leaderboard (views ~1461, movement ~1590) | Unchanged. It only renders with seasons off. |
| 3 | Match detail deltas (`_elo_deltas` ~285-330) and win % (`get_win_probability` elo.py ~120) | `ratings.match_deltas(match, scope, system=S)` and `ratings.win_probability(...)` |
| 4 | match_detail.html copy (~58, 73, 80, 108-124) | Neutral wording |
| 5 | Dashboard "Elo if true" (views ~837, dashboard.html ~92) | `ratings.projected_change_for(..., system=S)` |
| 6 | Dashboard `elo_gap` (views ~889) | Not read by any template, so delete it |
| 7 | Player detail format table (views ~625) | `player_format_ratings(player, system=S)`. Cells gain `rd` and `reason`. |
| 8 | Player detail stat cards (player_detail.html ~152-164) | All-time singles r ± RD, and the Glicko peak |
| 9 | Player detail chart (views ~597-616, template ~240, 741-777) | `ratings.rating_history(player, system=S)`, read from the RatingChange glicko columns. It has its own cache key. |
| 10 | Head-to-head prediction (views ~1873/1884, head_to_head.html ~69-82) | `ratings.win_probability`, computed outside the cached blob. The h2h charts plot points and are unchanged. |
| 11 | Calendar (views ~2334) and scheduled detail (~2391) win % | `ratings.win_probability` |
| 12 | Nav rating and rank (context_processors.py ~99-103, base.html ~183, 270) | `ratings.nav_reading(player, system=S)`: current-season singles (else all-time) r ± RD and its rank in that table |
| 13 | Push "Elo 1512 (+11)" and overtakes (notifications.py ~77-136) | The system is resolved per recipient. Glicko text uses RatingChange glicko columns or season rows. |
| 14 | Season rollover freeze (season_rollover.py ~199-214) and archive_detail.html ~55 | With `system(None)` = Glicko, rank by `glicko_rating` and stamp `rating_system` and `rd` on the standing. The archive shows ± when the standing is Glicko. |
| 15 | Championship seeding | Nothing to do: the seed is manual |
| 16 | Achievements (achievements.py ~373-439, achievement_definitions.py ~74-131) | Neutral copy only |

## ratings.py API
- Constants `ELO`, `GLICKO`, `GLICKO_FLAG`; `system(user)`.
- `rating_status(rated, *, experience=None, rd=None, system)` returns the verdict plus `reason`. Elo is unchanged.
- `display_rd(row, today)` gives `glicko2.inflate` from `last_rated_on` to today. It is display-only and is the only place
  that reads "now". The preview never reads now. The leaderboard cache stores `rd` and `last_rated_on`, never the verdict.
- `leaderboard(scope, top_x, *, system)`, `player_format_ratings(player, *, system)`,
  `applied_changes(match, scope, *, system)`, `projected_change_for(match, player, scope, *, system)`
  (`glicko_adapter.projected_glicko_changes`, per-player `updates`), `match_deltas(match, scope, *, system)`,
  `win_probability(side1, side2, *, system, scope, match=None)`:
  - a confirmed match uses `glicko_old_rating` and `glicko_rd_before` from RatingChange;
  - otherwise it uses `glicko_reading` on the match or scheduled day, through a new `elo.LiveBook.for_players(ids, season, fmt)`;
  - the result is `glicko2.win_probability`.
- `rating_history(player, *, system, fmt=SINGLES)`, `nav_reading(player, *, system)`, `scope_for(club_id, day, is_double)`.
- **`system` is a required keyword everywhere**, so a forgotten call site errors instead of quietly showing Elo.

## Cache
- All key builders live in `cache_utils`. Elo keys keep their EXACT current shape; Glicko keys add `g2_`.
  - `leaderboard_key(scope, top_x, system)` gives `{club}leaderboard_{gen}_{g2_}season{id}_{fmt}_{top}`.
  - `player_rank_key(player, system)`.
  - `rating_history_key(player, system)`.
- `invalidate_match_caches` and `invalidate_player_caches` delete both variants.
- Saving or deleting a `FeatureFlagOverride` named `glicko2` or `seasons` bumps that user's clubs' generation
  (signals.py ~217).
- New `cache_control --bump-leaderboard` bumps every club; the runbook uses it after an env flip.

## No-mixing guarantee
- Views emit neutral keys (`rating`, `rd`, `delta`, `status`).
- Rating components carry `data-rating-system`.
- A grep test allows Glicko columns to be read only in ratings.py and season_rollover.py, plus the write side
  (elo.py, elo_replay.py, glicko_adapter.py, rating_divergence.py). It replaces the no-readers test in
  test_glicko_shadow.py.

## Templates (chunk B)
- `_elo_delta.html` gains `system`. Glicko shows "+14 if agreed".
- New `_rating_value.html`: `1532` with a muted `±48` plus visually-hidden text "give or take 48". Uncertainty is shown in text, never by colour or opacity.
- `_rating_status.html`: Provisional stays a bordered `label-cap`, never amber.
- `_rating_help.html` is rewritten per system and links to the explainer page.
- `_leaderboard_season_results.html` moves to the neutral keys.
- Also update: base.html nav, the player_detail cards and chart (an RD band from two datasets with `fill`, the player hue
  via `player_hues.hue_hex`, axis colours as literal rgb()), head_to_head prediction, match_detail, dashboard and archive_detail.
- Icons: reuse `info` and `book-open`. Any new icon means `npm run icons` and committing the sprite.
- **"How ratings work"**: a `TemplateView` at `/pingpong/ratings/` named `how_ratings_work`. It 404s when the system is Elo,
  so it fails closed. It is linked from `_rating_help`, the leaderboard foot, player detail and the nav menu. Copy outline:
  1. The number is your estimated level, and the ± is how sure we are.
  2. Ranking is by the number alone.
  3. Each confirmed match is rated on its own, and the preview is exactly what will be written.
  4. Uncertainty grows while you don't play, so a returning player moves faster.
  5. Provisional means the ± is wider than N.
  6. Singles and doubles are rated separately, and a season starts from your all-time rating pulled toward 1500.
  7. Why the number changed at cutover: a new system, not a reset.

  One-line explanations:
  - High RD: "Few rated matches yet, so each result moves this a lot."
  - Returning: "Not played since {date}, so the next results count extra while it catches up."

## Runbook (chunk B): `docs/glicko2-cutover.md`, in the style of `docs/tenancy-rollout.md`
**Prerequisites**
- KAN-68 parameters are fitted.
- The live-vs-replay check is clean (`recalculate_elo --dry-run` shadow diff).
- The rehearsal on the Supabase clone has been done.
- A DB snapshot has been taken.

**Steps**
1. Deploy with both flags off and migrations 0044/0045. Verify nothing visible changed.
2. In a quiet window (this is not safe during live confirmations), run `recalculate_elo --dry-run`, then
   `--format singles` and `--format doubles`. Verify there are no NULL `glicko_change` values.
3. Create the first season in the admin.
4. Add staff overrides for `seasons` and `glicko2`, run the smoke checklist, confirm unflagged users see no change, and soak for at least a day.
   Do not let the soak cross a season end.
5. Post the announcement. Set `TTSTATS_FLAGS_ON=seasons,glicko2`, restart, run `cache_control --bump-leaderboard`, and smoke test again.

**Rollback:** drop `glicko2` from the env or overrides and bump. Elo is current because both systems kept writing.

**Point of no return**
- The hard one is KAN-73, when Elo writes stop. Do it no earlier than the end of the first full season.
- A soft one is the first rollover: it freezes a Glicko standing, which is labelled and survives a rollback.
- Pushes already sent with Glicko numbers cannot be recalled.

**Announcement draft:** "Ratings now use Glicko-2 and seasons start today. Your number shows a ± (how sure we
are). Expect numbers to shift; rankings largely hold."

## Tests
**Chunk A (pytest)**
- A truth table for `system()` over all four flag combinations, plus overrides and `system(None)`.
- Preview equals written: compare against `RatingChange.glicko_change` for all-time, season, the season seed and a NULL season
  row, singles and doubles.
- Win probability from the stored pre-match values, singles and doubles.
- Leaderboard: sort order, NULL Glicko counted as unrated, movement, peak, and a flat query count in both systems.
- Status: the RD threshold read at call time, and the "returning" reason.
- Writers: LiveBook, RatingWriter, the historic-model guard, backfill idempotency, and a Glicko failure giving NULL with Elo
  intact. `test_match_save_performance` stays flat.
- Cache: keys differ by system, Elo keys are byte-identical, both variants are invalidated, an override save bumps the generation,
  and `--bump-leaderboard` works.
- Rollover and archive under Glicko. Push text per recipient.

**Chunk B**
- Rendered no-mixing sweep, parametrised over every surface: Glicko pages contain no "Elo" and carry
  `data-rating-system="glicko2"`, and Elo pages contain no "±". The existing suite stays green.
- e2e:
  - `seed_e2e` gains an `e2e-glicko` user with both overrides and pipeline-written state, and `auth.setup.js` gains `glicko.json`.
  - `glicko-cutover.spec.js` (iphone-safari, `applyProdCSP`, no page errors):
    - the leaderboard shows ± and provisional;
    - the htmx season and format switch stays Glicko;
    - the pending delta matches between dashboard and match detail;
    - the chart draws;
    - the h2h prediction, the nav ± and the help link all work.
  - `.dark.spec.js`: markers are not amber, and axes are rgb.
  - `.nojs.spec.js`.
  - `.desktop.spec.js`: the sidebar rating.
  - The existing e2e and e2e-seasons specs assert there is no ±.

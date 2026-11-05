# Glicko-2 read cutover runbook (KAN-71 / KAN-72)

**What this changes:** the rating every member sees (Elo becomes Glicko-2 with a
±), and it turns seasons on for them. Elo keeps being written the whole time, so
the change is reversible by flags until KAN-73 stops the Elo writes.

Contract: `docs/decisions/kan-63-glicko2.md`. Implementation plan:
`docs/decisions/kan-63-cutover-plan.md`. Style and host setup follow
`docs/tenancy-rollout.md` (same VPS, same `compose.prod.yml`).

**Do not reorder the steps.** The two failure modes are quiet: NULL Glicko state
makes players start from 1500 / RD 350 at their next match, and a stale
leaderboard cache renders blank numbers for flagged users.

Set these once per session (as in the tenancy runbook):

```bash
cd "$WORK_DIR"
alias dcp='docker compose -f compose.prod.yml'
alias dweb='docker compose -f compose.prod.yml exec -T web python manage.py'
```

---

## Prerequisites (all four, or stop)

1. **KAN-68 parameters are fitted** (`glicko_adapter.DEFAULT_PARAMS`: tau 0.5,
   7-day period, season RD floor 100) and merged.
2. **The KAN-69 gate is clean.** After at least one real session of live play on
   the build that writes the shadow, `dweb recalculate_elo --dry-run` shows
   **zero unexplained shadow diffs** (stored vs replayed `glicko_rating`, `rd`,
   `volatility`, `last_rated_on`). Diffs listed as *explained* (matches confirmed
   out of date order) are fine; any unexplained diff means live and replay
   disagree, which is a bug. Do not proceed.
3. **The rehearsal on the Supabase clone has been done** end to end, steps 1-5
   below, including the smoke checklist.
4. **A database snapshot has been taken** (same procedure as the tenancy
   runbook). Take another immediately before step 2.

## Steps

### 1. Deploy with both flags off

Deploy the release containing migrations 0044 and 0045 with `seasons` and
`glicko2` **off** (no `TTSTATS_FLAGS_ON` entry, no overrides).

Immediately after the deploy, before anything else:

```bash
dweb cache_control --bump-leaderboard
```

Leaderboard entries cached before the deploy lack the neutral `rating` / `peak`
keys and would render blank for anyone with a `seasons` override. Bumping every
club's generation drops them.

Verify nothing visible changed: log in as an ordinary member, open the Table, a
profile and a match. Everything looks as yesterday and there is no "±" anywhere.

### 2. Backfill Glicko state, in a quiet window

**Not safe during live confirmations**: `recalculate_elo` holds `select_for_update`
on every Player row in one transaction. Pick an hour nobody is playing, and run
it **outside a deploy** (the ssh-action has a command timeout).

```bash
dweb recalculate_elo --dry-run                 # look first; writes nothing
dweb recalculate_elo --format singles          # rebuilds singles rows + Glicko history
dweb recalculate_elo --format doubles          # doubles are rated too (KAN-70)
```

`--format` leaves `Player.elo_*` and `EloHistory` alone and is idempotent; a
failure part-way can simply be re-run.

Verify there are no NULL `glicko_change` values (a NULL is a skipped projection):

```bash
dcp exec -T db psql -U pingpong -d pingpong -c \
  "SELECT count(*) FROM pingpong_ratingchange WHERE glicko_change IS NULL;"
```

Expect `0`. If not, investigate before going on (a historic match with an empty
side or a player on both sides is logged and skipped).

### 3. Create the first season

In the admin, create the club's season (a running one that contains today).
Season ratings are written at confirmation only, never back-filled; the admin
prints the `recalculate_elo --season <id>` command if you want past matches in
it. Until the season has two rated players the Table shows all-time (stated on
the page).

### 4. Staff overrides, smoke, soak

Add `FeatureFlagOverride` rows for staff accounts: `seasons` **and** `glicko2`
both enabled. Either alone shows Elo (a system check warns when `glicko2` is on
without `seasons`).

Run the smoke checklist (below) as a flagged staff member, then as an unflagged
member to confirm they see **no change**. Soak for **at least one day**.
**Do not let the soak cross a season end**: the rollover freezes a Glicko
standing (see Point of no return).

### 5. Announce, then flip for everyone

Post the announcement (draft below). Then:

```bash
# .env.prod
TTSTATS_FLAGS_ON=seasons,glicko2      # keep any flags already listed
```

```bash
dcp up -d web                          # restart: settings defaults need it
dweb cache_control --bump-leaderboard  # every club, after ANY env flip
```

Run the smoke checklist again as an ordinary, previously unflagged member.

## Smoke checklist

Flagged member, phone width, each page free of the word "Elo" and showing a ±
where a rating is shown:

- [ ] **Table** (default): numbers carry a muted `±n`; new players show
  **Provisional**, players back after a break show **Returning**; the footer links
  to **How ratings work**. Switch season and format: still Glicko, no reload.
- [ ] **Nav / menu**: rating line reads `1532 ±48 · 3rd`; the menu has a
  *How ratings work* row.
- [ ] **Today**: a pending result reads `+14 if true` (no unit).
- [ ] **Match page**, pending: `+14 if agreed`. Confirm it: the same number, now
  without "if agreed".
- [ ] **Player page**: stat cards `Rating ±n` and `Peak rating`; the chart draws
  with a shaded band; the format table shows ± and markers.
- [ ] **Head to head**: the prediction reads "Current Ratings".
- [ ] **Calendar / scheduled match**: win chance shows.
- [ ] **Past seasons**: a Glicko-frozen standing shows ±; an Elo-frozen one does not.
- [ ] **/pingpong/ratings/** renders; for an unflagged member it is a **404**.
- [ ] An unflagged member sees none of the above (no ±).

## Rollback

Drop `glicko2` from `TTSTATS_FLAGS_ON` (and/or disable the overrides), restart
`web`, and run `dweb cache_control --bump-leaderboard`. **Elo is current,
because both systems kept writing**, so members are back on today's numbers with
nothing to rebuild. Overrides take effect within 30s and bump the clubs'
leaderboard themselves; the env flip needs the restart and the manual bump.

## Point of no return

- **Hard: KAN-73**, when Elo writes stop. After it, rollback leaves stale Elo.
  Do it no earlier than the end of the first full season on Glicko.
- **Soft: the first rollover.** It freezes a Glicko `SeasonStanding`
  (labelled with its `rating_system`, so it survives a rollback and keeps its ±).
- **Pushes already sent** with Glicko numbers cannot be recalled.

## Known gap

**Achievements stay on Elo until KAN-73.** Giant Slayer and Peak Performer are
still decided from `EloHistory`; only their wording is neutral under Glicko
("points above you", "rating peak"). A member can therefore earn or miss one in
a way that does not match the number they see. Acceptable for the soak; do not
announce achievements as rating-based.

## Announcement draft

> Ratings now use Glicko-2 and seasons start today. Your number shows a ± (how
> sure we are). Expect numbers to shift; rankings largely hold. Tap *How ratings
> work* in the menu for the short version.

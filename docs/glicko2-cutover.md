# Glicko-2 read cutover runbook (KAN-71 / KAN-72)

**What this changes:** the rating every member sees (Elo becomes Glicko-2 with a
±), and it turns seasons on for them. Elo keeps being written the whole time, so
the change is reversible by flags until KAN-73 stops the Elo writes.

Contract: `docs/decisions/kan-63-glicko2.md`. Implementation plan:
`docs/decisions/kan-63-cutover-plan.md`. Style and host setup follow
`docs/tenancy-rollout.md` (same VPS, same `compose.prod.yml`).

**There are two phases and the order inside them matters.** Phase 0 (shadow)
changes nothing a member can see; it only fills in the Glicko numbers that are
written next to Elo. Phase 1 (cutover) is the flag flip. The failure modes are
quiet: NULL Glicko state makes players start from 1500 / RD 350 at their next
match, a stale leaderboard cache renders blank numbers for flagged users, and a
bare `dcp up -d web` redeploys the wrong image (see "Restarting web").

Set these once per session (as in the tenancy runbook):

```bash
export WORK_DIR=/root/ttstats
cd "$WORK_DIR"
alias dcp='docker compose -f compose.prod.yml'
alias dweb='docker compose -f compose.prod.yml exec -T web python manage.py'
```

## Backup and restore

Take a snapshot at the start of phase 1, and again immediately before any step
you cannot undo by a flag. Same procedure as `docs/tenancy-rollout.md`:

```bash
# On the VPS. Reads the credentials out of the container's own environment,
# so there is nothing to mistype.
dcp exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
  | gzip > ~/ttstats-$(date +%Y%m%d-%H%M).sql.gz

ls -lh ~/ttstats-*.sql.gz    # verify it is non-empty before proceeding
```

Restore (only if a step's rollback says so; it discards everything written
since the snapshot, including real matches):

```bash
dcp stop web
gunzip -c ~/ttstats-YYYYmmdd-HHMM.sql.gz \
  | dcp exec -T db sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'
# then start web the pinned way, see "Restarting web"
```

## Restarting web

**Never a bare `dcp up -d web`.** `compose.prod.yml` takes the image from
`WEB_IMAGE` (default `ghcr.io/eyedema/ttstats-web:latest`) and the Sentry
release from `SENTRY_RELEASE`; the deploy job sets both to the commit SHA.
A bare `up` in a fresh shell falls back to `:latest` and an empty release,
which can silently swap the running build. Read what is running, then pass it
back:

```bash
IMAGE=$(docker inspect --format '{{.Config.Image}}' "$(dcp ps -q web)")
echo "$IMAGE"                       # ghcr.io/eyedema/ttstats-web:<sha>; check it is the deployed one
WEB_IMAGE="$IMAGE" SENTRY_RELEASE="${IMAGE##*:}" dcp up -d web
```

If the tag is not a commit SHA, set `SENTRY_RELEASE=<sha>` by hand.
(After a restore, `dcp stop web` removes nothing: `dcp ps -q web` still finds
the stopped container, so run the first line before the restore if in doubt.)

---

## Phase 0: shadow (changes nothing visible)

### 0.1 Deploy with both flags off

Deploy the release containing migrations 0044 and 0045 with `seasons` and
`glicko2` **off** (no `TTSTATS_FLAGS_ON` entry, no overrides). The normal deploy
job does this.

Immediately after the deploy, before anything else:

```bash
dweb cache_control --bump-leaderboard
```

Leaderboard entries cached before the deploy lack the neutral `rating` / `peak`
keys. The view back-fills them, but bumping every club's generation drops them
anyway.

Verify nothing visible changed: log in as an ordinary member, open the Table, a
profile and a match. Everything looks as yesterday and there is no "±" anywhere.

### 0.2 Backfill the Glicko state, in a quiet window

**Not safe during live confirmations**: `recalculate_elo` holds `select_for_update`
on every Player row in one transaction. Pick an hour nobody is playing, and run
it **outside a deploy** (the ssh-action has a 10-minute command timeout).

```bash
dweb recalculate_elo --format singles    # rebuilds singles rows + Glicko history
dweb recalculate_elo --format doubles    # doubles are rated too (KAN-70)
```

`--format` leaves `Player.elo_*` and `EloHistory` alone and is idempotent; a
failure part-way can simply be re-run.

**A `--dry-run` BEFORE the backfill is expected to diff every row**: the stored
shadow columns are NULL and the replay has values. That is not a failure; the
dry-run only means something after this step.

Verify there are no NULL `glicko_change` values (a NULL is a skipped projection):

```bash
dcp exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM pingpong_ratingchange WHERE glicko_change IS NULL;"'
```

Expect `0`. If not, investigate before going on (a historic match with an empty
side or a player on both sides is logged and skipped).

### 0.3 Shadow window and the KAN-69 gate

Leave the build running with the shadow being written live, then compare live
against replay.

- **Gate:** wait until **at least one real club session has been confirmed live**
  on this build. **Proposed 2026-11-05; the ticket's original gate ("1 month /
  50 matches") stands until the product owner confirms the shorter one.** Do not
  treat the shorter gate as agreed.
- Then:

  ```bash
  dweb recalculate_elo --dry-run
  ```

  must show **zero unexplained shadow diffs** (stored vs replayed
  `glicko_rating`, `rd`, `volatility`, `last_rated_on`). Diffs listed as
  *explained* (matches confirmed out of date order) are fine; any unexplained
  diff means live and replay disagree, which is a bug. Do not proceed.

Other prerequisites, all of them, or stop:

1. **KAN-68 parameters are fitted** (`glicko_adapter.DEFAULT_PARAMS`: tau 0.5,
   7-day period, season RD floor 100) and merged.
2. **The rehearsal on the Supabase clone has been done** end to end, phases 0
   and 1, including the smoke checklist.

---

## Phase 1: cutover

### 1.1 Snapshot

Take the snapshot (see "Backup and restore") and confirm the file is non-empty.

### 1.2 The first season, only if the club has none

Check the admin Seasons list. **Seasons tables already exist in prod with the
flag off**, and seasons must not overlap (a database exclusion constraint): if
the club already has a season covering today, use it and create nothing. Only if
there is none, create one (a running season that contains today). Season ratings
are written at confirmation only, never back-filled; the admin prints the
`recalculate_elo --season <id>` command if you want past matches in it. Until the
season has two rated players the Table shows all-time (stated on the page).

### 1.3 Staff overrides, then a soak

Add `FeatureFlagOverride` rows for staff accounts: `seasons` **and** `glicko2`
both enabled. Either alone shows Elo (a system check warns when `glicko2` is on
without `seasons`).

Run the smoke checklist (below) as a flagged staff member, then as an unflagged
member to confirm they see **no change**. Soak for **at least one day**.

**Do not let the soak cross a season end.** During an override-only soak the
jobs have no user, and `system(None)` reads the settings, which are still Elo. A
season that ends during the soak is therefore archived and frozen **in Elo for
everyone**, including the flagged staff, and that frozen standing cannot be
redone (see Point of no return).

### 1.4 Announce, then flip for everyone

Post the announcement (draft below). Then edit `.env.prod`:

```bash
# .env.prod
TTSTATS_FLAGS_ON=seasons,glicko2      # keep any flags already listed
```

Restart web the pinned way (see "Restarting web"), then bump:

```bash
IMAGE=$(docker inspect --format '{{.Config.Image}}' "$(dcp ps -q web)")
WEB_IMAGE="$IMAGE" SENTRY_RELEASE="${IMAGE##*:}" dcp up -d web
dweb cache_control --bump-leaderboard   # every club, after ANY env flip
```

### 1.5 Smoke

Run the smoke checklist again as an ordinary, previously unflagged member.

## Smoke checklist

Flagged member, phone width, each page free of the word "Elo" and showing a ±
where a rating is shown:

- [ ] **Table** (default): numbers carry a muted `±n`; new players show
  **Provisional**, players back after a break show **Returning** with "Not played
  since"; the footer links to **How ratings work**. Switch season and format: still
  Glicko, no reload.
- [ ] **Nav / menu**: rating line reads `1532 ±48 · 3rd`; the menu has a
  *How ratings work* row.
- [ ] **Today**: a pending result reads `+14 if agreed` (no unit).
- [ ] **Match page**, pending: `+14 if agreed`. Confirm it: the same number, now
  without "if agreed". A **doubles** match shows one figure per player.
- [ ] **Player page**: stat cards `Rating ±n` and `Peak rating`; the chart draws
  with a shaded band; the format table shows ± and markers.
- [ ] **Head to head**: the prediction reads "Current Ratings" with both ratings and ±.
- [ ] **Calendar / scheduled match**: win chance shows.
- [ ] **Past seasons**: a Glicko-frozen standing shows ±; an Elo-frozen one does not.
- [ ] **/pingpong/ratings/** renders; for an unflagged member it is a **404**.
- [ ] An unflagged member sees none of the above (no ±).

## Rollback

**Full rollback is removing BOTH `glicko2` and `seasons` from `TTSTATS_FLAGS_ON`**
(and disabling the overrides), which puts every member back on exactly today's
screens. Dropping only `glicko2` leaves the seasons-plus-Elo UI (season tables,
format ratings), which is **not** today's screen.

1. Edit `.env.prod`: remove `glicko2` and `seasons` from `TTSTATS_FLAGS_ON`.
2. Restart web the pinned way (see "Restarting web"; never a bare `dcp up -d web`).
3. `dweb cache_control --bump-leaderboard`.

**Elo is current, because both systems kept writing**, so members are back on
today's numbers with nothing to rebuild. Overrides take effect within 30s and
bump the clubs' leaderboard themselves; the env flip needs the restart and the
manual bump.

## Point of no return

- **Hard: KAN-73**, when Elo writes stop. After it, rollback leaves stale Elo.
  Do it no earlier than the end of the first full season on Glicko.
- **Soft: the first rollover.** It freezes a Glicko `SeasonStanding`
  (labelled with its `rating_system`, so it survives a rollback and keeps its ±).
- **Pushes already sent** with Glicko numbers cannot be recalled.

## Known gap

**Achievements stay on Elo until KAN-73.** Giant Slayer and Peak Performer are
still decided from `EloHistory`; only their wording is neutral under Glicko
("well above you", "rating peak"; the Elo-scale point thresholds are dropped from
the copy). A member can therefore earn or miss one in a way that does not match
the number they see. Acceptable for the soak; do not announce achievements as
rating-based.

## Announcement draft

> Ratings now use Glicko-2 and seasons start today. Your number shows a ± (how
> sure we are). Expect numbers to shift; rankings largely hold. Tap *How ratings
> work* in the menu for the short version.

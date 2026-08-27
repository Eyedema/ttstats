# Tenancy rollout and rollback runbook (KAN-6 / T1.12)

**What this changes:** what every user can see. Against a live database, on a
single host.

The nearest thing to a staging environment is the **Supabase clone of
production** — steps 1–3 have been rehearsed against it (see the last section).
Use it before touching the VPS.

The failure mode is silent. A tenancy leak does not raise, does not 500 and
does not appear in any log — a user simply sees a name that is not theirs. The
opposite failure is louder and worse: a row left with a null club after the
managers start filtering is invisible to everyone, which users experience as
their data having vanished.

**The order of operations is the difference between an invisible change and
everyone's data appearing to disappear.** Do not reorder the steps.

---

## Before you start

**Production is the `db` service in `compose.prod.yml` — PostgreSQL 16, volume
`pgdata`, on the VPS.** Supabase holds only a *dev clone*, pulled by `pg_dump`
from that container. Every command below targets the container. (This was
KAN-22's open question; recorded here so nobody has to re-derive it at 23:00.)

Deploy is GitHub Actions → SSH → `docker compose -f compose.prod.yml`, pinned
to a per-commit image tag, so **a rollback is re-running an older deploy rather
than a rebuild**.

You will need: SSH to the VPS, the repo checked out in `$WORK_DIR` there, and
the ability to edit `.env.prod`.

Set these once per session:

```bash
cd "$WORK_DIR"
alias dcp='docker compose -f compose.prod.yml'
alias dweb='docker compose -f compose.prod.yml exec -T web python manage.py'
```

### Three constraints that have already caused incidents

- **The VPS is memory-constrained** (3.7GB, swap added). The backfill competes
  with the running app. Run it out of hours and lower `--batch-size` if the box
  is under pressure.
- **`ssh-action`'s deploy step has a 20m `command_timeout`.** The backfill must
  **not** run inside a deploy. It is a separate, manually invoked command, which
  is exactly why `backfill_clubs` exists alongside the data migration.
- **`entrypoint.sh` runs `collectstatic` under `set -e`.** A vendored file
  missing its `.map` sibling fails the *deploy*, not just the page. Confirm CI's
  asset build was green before starting a migration sequence.

### Snapshot procedure

Take one before **step 3** and before **step 7**. Both are the steps where a
mistake is not fixable by reversing a migration.

```bash
# On the VPS. Reads the credentials out of the container's own environment,
# so there is nothing to mistype.
dcp exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
  | gzip > ~/ttstats-$(date +%Y%m%d-%H%M).sql.gz

ls -lh ~/ttstats-*.sql.gz    # verify it is non-empty before proceeding
```

Restore (only if a step's rollback says so):

```bash
dcp stop web
gunzip -c ~/ttstats-YYYYmmdd-HHMM.sql.gz \
  | dcp exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
dcp start web
```

> **Known limitation (KAN-75).** A prod-shaped Postgres cannot currently be
> built by running the migration chain from scratch — `0010_cleanup_games`
> fails with a duplicate index name on PostgreSQL. A rehearsal environment can
> therefore only be produced by *restoring a dump*, never by migrating an empty
> database. This does not affect any step below, all of which run against a
> database that is already past 0010.

---

## Step 1 — Deploy the feature flags (KAN-11)

Migration `0031_feature_flag_overrides`. No behaviour change: nothing reads
`clubs` yet.

**Verify**

```bash
dweb showmigrations pingpong | tail -5      # 0031 marked [X]
dweb shell -c "from pingpong import flags; print(flags.all_flags())"
# -> {'clubs': False, 'tenancy_canary': False}
```

**Gate before step 2:** both flags report `False`.

**Rollback:** `dweb migrate pingpong 0030`, then re-deploy the previous image.

---

## Step 2 — Deploy the club columns (KAN-12)

Migration `0032_club_models`. Additive and inert: nullable, indexed FKs that
nothing reads.

**Verify**

```bash
dweb shell -c "
from pingpong.models import Player, Match
print('players', Player.objects.count(), 'matches', Match.all_objects.count())
print('club column present:', 'club' in [f.name for f in Player._meta.get_fields()])
"
```

Then load the app in a browser. Every page must render exactly as before.

**Gate before step 3:** row counts unchanged from step 1, app renders normally.

**Rollback:** `dweb migrate pingpong 0031`. Verified two-way against
PostgreSQL 16 with rows present; row counts are identical at every step.

---

## Step 3 — Run the backfill (KAN-13)

**Take a snapshot first.** This is the highest-consequence data change in the
epic.

Migration `0035_backfill_clubs` runs as part of the deploy's `migrate`, and it
is deliberately **last in the chain** — it writes `Player.club`, `Match.club`
*and* `ClubMembership.role`, so every column it touches has to exist first.

> It used to be `0033`, ahead of the roles migration, which is exactly the
> mistake this ordering prevents: a migration runs against *historical* models,
> so the `role` column was not there yet and the run died with
> `TypeError: ClubMembership() got unexpected keyword arguments: 'role'`.
> It reached a real database because the failing line only runs when there are
> Users, and the test database has none.

If the dataset ever grows enough for the in-deploy run to be a concern, deploy
and run `backfill_clubs` separately instead — it is the same code.

```bash
dweb backfill_clubs --dry-run      # read the summary before writing anything
dweb backfill_clubs
```

**Verify — this is the most important check in the runbook**

```bash
dweb shell -c "
from django.contrib.auth.models import User
from pingpong.models import *
print('null clubs:',
      Player.objects.filter(club__isnull=True).count(),
      Match.all_objects.filter(club__isnull=True).count(),
      ScheduledMatch.all_objects.filter(club__isnull=True).count(),
      Championship.all_objects.filter(club__isnull=True).count(),
      Location.objects.filter(club__isnull=True).count())
print('users', User.objects.count(), 'memberships', ClubMembership.objects.count())
print('owners', ClubMembership.objects.filter(role='owner').count())
"
dweb backfill_clubs            # run twice: must say 'Nothing to do'
```

**Gate before step 4 — all of these:**

- every null-club count is **0**
- `memberships` **≥** `users` (every user is in a club; without this the app is
  empty for everyone once the flag flips)
- `owners` is **1**
- the second run reports `Nothing to do — every row already has a club`
- row counts match the snapshot

Review any anomalies the command printed. They are assigned, not skipped, so
nothing is hidden — but a match reported as spanning clubs is worth a look.

**Rollback:** `dweb migrate pingpong 0034` (clears every assignment, deletes
the memberships and the default club — nulls the FKs *before* deleting the club,
because they are all `PROTECT`). Restore the snapshot only if that fails.

---

## Step 4 — Deploy the scoping code, flag OFF (KAN-14/15/16/21)

Migrations `0033_club_roles` and `0034_club_invites`. The managers, cache keys,
roles, invites and canary all ship here, **inert**: `clubs` is still `False`, so
every query is byte-identical to pre-tenancy.

> **In practice steps 1–4 are a single deploy.** One `migrate` applies
> `0031`→`0035` in order, ending with the backfill. The steps are kept separate
> because their *gates* and *rollbacks* differ — run step 3's verification block
> after that deploy regardless, since it is the check that matters.

**Verify**

```bash
dweb shell -c "from pingpong import flags; print(flags.is_enabled('clubs'))"   # False
```

Walk the smoke checklist below. Nothing should look different at all.

**Gate before step 5:** flag reports `False`, smoke checklist passes, no new
errors in `dcp logs --tail=200 web`.

**Rollback:** re-run the previous deploy (the image is pinned per commit). The
migrations may stay applied — they are additive.

---

## Step 5 — Enable for staff only

**No deploy.** This is the main reason the flag exists.

```bash
dweb shell -c "
from django.contrib.auth.models import User
from pingpong.models import FeatureFlagOverride
u = User.objects.get(username='YOUR_USERNAME')
FeatureFlagOverride.objects.update_or_create(
    name='clubs', user=u,
    defaults={'enabled': True, 'note': 'tenancy rollout, staff phase'})
"
```

Also arm the leak canary for this phase — the one time it is worth paying for:

```bash
# .env.prod
TENANCY_CANARY_SAMPLE_RATE=0.02
TTSTATS_FLAGS_ON=tenancy_canary
```

```bash
dcp up -d web        # restart to pick up the env
```

**Verify:** as the overridden user, walk the full smoke checklist. Then:

```bash
dcp logs --since=15m web | grep 'TENANCY CANARY'    # expect no output
```

**Gate before step 6 — all of these:**

- smoke checklist passes as the flagged user
- another (unflagged) user still sees the app exactly as before
- **no** `TENANCY CANARY` lines
- soak for at least one real session — a day is better than an hour

**Rollback:** delete the override row. Takes effect on the next request; no
deploy, no restart.

---

## Step 6 — Enable for everyone

```bash
# .env.prod
TTSTATS_FLAGS_ON=clubs,tenancy_canary
```

```bash
dcp up -d web
dweb shell -c "from pingpong import flags; print(flags.is_enabled('clubs'))"   # True
```

**Verify**

```bash
dcp logs --since=1h web | grep 'TENANCY CANARY'     # expect no output
dweb cache_control --stats                          # keys grouped by club namespace
```

Ask one real user to confirm their matches and the leaderboard look right. The
canary cannot detect a filter applied to the *wrong* club; a human can.

**Gate before step 7:** a week of no canary lines and no user reports.

**Rollback:** remove `clubs` from `TTSTATS_FLAGS_ON`, `dcp up -d web`. Previous
behaviour returns within one request. **No deploy and no migration** — this is
what the whole flag apparatus is for.

---

## Step 7 — Make the club columns non-null

**Only after a real soak.** This is the point of no return: reversing it means
re-adding NOT NULL columns with nothing to put in them.

**Take a second snapshot first.**

```bash
dweb backfill_clubs        # must report 'Nothing to do' before you begin
```

This migration does not exist yet — write it when you get here, as its own
migration so it can be reverted independently of everything above.

**Rollback:** restore the snapshot. There is no other route, which is why the
soak is not optional.

---

## Smoke checklist

Run as a real user on a real phone, not just curl.

- [ ] **Today** — pending confirmations and rivalries look right
- [ ] **Play** — the player picker offers *your* club and nobody else's
- [ ] **Table** — leaderboard shows your club's players, movement arrows render
- [ ] **Cups** — championships list, one detail page with standings
- [ ] **Match detail** — a confirmed match, and one awaiting confirmation
- [ ] **Live scoreboard** — start a match, score a point, undo it
- [ ] **Push** — trigger one notification and confirm it arrives
- [ ] **Drawer** — opens *and closes*; club name visible in the header
- [ ] **All matches / Everyone / Calendar / Head to head** — all render

---

## Edge cases to expect

- **A user mid-session when the flag flips.** Their next request resolves an
  active club and is scoped. Nothing is cached across the boundary: cache keys
  carry the club namespace, and the flag-off keys are a different namespace
  entirely, so a stale pre-flip entry can never be served to a post-flip
  request.
- **A live match in progress during the cutover.** The scoreboard holds state
  in `Match.live_state` and the client keeps posting. The backfill assigns live
  matches too (the command uses `all_objects`), so the scorekeeper keeps
  reaching theirs.
- **Rows created between the backfill and the flag flip.** Re-run
  `dweb backfill_clubs` — that is what it is for. Do it immediately before
  step 5, and again before step 6.
- **Push subscriptions spanning the cutover.** Keyed on user, not club.
  Unaffected.
- **A partial deploy** (web restarted, migrate did not finish). `migrate` is
  transactional per migration on PostgreSQL. Re-run the deploy; check
  `showmigrations` before assuming anything.

---

## What has and has not been rehearsed

**Rehearsed against the Supabase copy of production** (3 users, 4 players, 101
matches, 389 games, 202 participants, 2 locations) — steps 1 through 3 forward,
then the repair command twice:

| check | result |
|---|---|
| null clubs, all five tables | **0** |
| memberships vs users | **3 / 3** |
| owners | **1** (roles: 1 owner, 2 admin, 0 member) |
| row counts vs before | **identical** |
| `backfill_clubs` re-run, twice | `Nothing to do — every row already has a club` |

**The step-3 rollback was then rehearsed on the same copy** — reverse, verify,
re-forward:

| after `migrate pingpong 0034` | result |
|---|---|
| application rows (users, players, matches, games, locations, participants) | **all intact** |
| clubs / memberships | **0 / 0** — tenancy state fully cleared |
| rows still pointing at a club | **0** — no `ProtectedError`, the FKs are nulled before the club row is deleted |
| `role` column | still present (0034 stays applied; reversing step 3 undoes the *data*, not the schema) |

Rolling forward again reproduced the identical end state: zero null clubs,
3/3 memberships, exactly 1 owner, row counts matching the original snapshot.
**The backfill is safe to reverse and re-apply on live data.**

Also rehearsed on a scratch PostgreSQL 16 with synthetic data, same result.

**Not yet rehearsed:** steps 5–7. The flag-flip rollback (steps 5–6) is
inherently a production action, and step 7's migration does not exist yet.

> The Supabase clone was briefly believed unreachable — the credentials in
> `.env.dev.local` are stale. Pass a working `DATABASE_URL` on the command line
> instead. It is a **clone**, not production: rehearsing against it is safe, and
> it is the closest thing to a staging environment this project has.

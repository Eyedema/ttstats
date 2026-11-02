---
paths:
  - "ttstats/ttstats/middleware.py"
  - "ttstats/pingpong/managers.py"
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/forms.py"
  - "ttstats/pingpong/context_processors.py"
  - "ttstats/pingpong/club_backfill.py"
  - "ttstats/pingpong/management/commands/backfill_clubs.py"
  - "ttstats/pingpong/tests/test_tenancy*.py"
---

# Tenancy: row-level security and the active club

### Row-Level Security

**Two independent boundaries, applied in this order.** Getting them backwards,
or conflating them, is the whole failure mode:

1. **The club** (tenancy) -- whose data is this? Flag-gated on `clubs`.
2. **The user** (participation) -- which of my club's rows am I party to?

- `CurrentUserMiddleware` stores user in thread-local (`_thread_locals.user`)
- Managers filter querysets: no user = unfiltered, anonymous = empty, staff = all, regular = own matches only
- **Staff bypass the user filter. NOTHING bypasses the club filter.** A staff
  account is global; a club is not.
- **"Green with the `clubs` flag on" means something specific.** Forcing the
  flag on for the *whole* suite fails ~200 tests, and that is not a defect:
  those fixtures build a pre-backfill world (a logged-in user with no club),
  where the designed answer is "see nothing". Production is never in that state
  once step 3 of `docs/tenancy-rollout.md` passes its gate. The real claim --
  **once a user has a club, the flag changes nothing they can see** -- is
  asserted directly by
  `test_tenancy_isolation.py::TestABackfilledWorldBehavesIdenticallyWithTheFlagOn`.
  Suites that are *about* tenancy opt the flag on per class.
- **All six managers share `ScopedManagerMixin`.** They used to be six
  near-duplicate copies of the same staff/anonymous/regular branch. Adding a
  second boundary to six copies is how one gets missed, and a missed one is a
  leak that raises nothing and logs nothing. Subclasses supply `club_lookup`,
  `_base_queryset()`, `_scope_to_user()`, and the two opt-out knobs.
- `filters_by_user = False` (PlayerManager, LiveMatchManager) skips the
  *participation* filter only. It must never skip the club filter.
- `anonymous_returns_none = False` exists solely to keep the flag-off path
  byte-identical where the pre-tenancy manager served anonymous visitors.
- **Bypass:** `Model.all_objects` / `_base_manager` skip row-level filtering
  (championship views legitimately need every match in a championship, not just
  the viewer's). **After tenancy they must not also skip the club filter** --
  wrap them in `managers.scope_to_active_club(qs)`. See the audit table below.
- **A `queryset=` written in a form class body is evaluated at import time**,
  with no user and no club in context. The manager runs unfiltered and its
  filters are then fixed forever. Harmless while `PlayerManager` returned
  everyone to everyone; with tenancy it is a leak *with a write attached*, since
  a picker offering another club's players lets a user build a cross-club match
  through the UI. `ScopedChoicesMixin` re-binds them per instantiation.

#### New rows are stamped with the viewer's club

**Scoping reads without wiring writes is a silent data-loss bug.** With `clubs`
on, a row created through the UI got `club=NULL`, and the scoped managers filter
on `club=<active club>` -- which NULL never matches. The thing you just created
was invisible the instant you created it. This shipped and was found during the
rollout rehearsal, not by the test suite, because every isolation fixture
assigns `club` explicitly and nothing exercised a *creation path*.

`ClubStampedMixin` (models.py) fills the column in `save()` for `Player`,
`Location`, `Match`, `ScheduledMatch` and `Championship`.

- **At the model layer, not per view.** Five scoped models are reachable from a
  dozen creation paths -- views, the admin, `register_entry`,
  `generate_schedule`, whatever gets added next -- and the failure mode of the
  per-view approach is the path somebody forgot, which is exactly how this was
  missed.
- **An explicit club always wins.** Stamping fills a blank; it never overrules a
  caller. The backfill and any future data fix depend on that.
- **No viewer means no stamping**, and **flag off means no stamping** -- the
  same two contracts the managers keep. Without the first, this would fight
  `backfill_clubs` for the column.
- A *null* club on an existing row is filled on save (the repair case). A row
  that already has one is **never** re-stamped: re-saving must not move data
  between clubs, or an admin edit becomes a migration.

#### The active club, and what `None` means

`get_current_club()` sits alongside `get_current_user()` in
`ttstats/middleware.py`. **`None` means two different things and the
difference is the entire security contract:**

- **No user in context** (management command, migration) -- there is no viewer,
  so queries stay *unfiltered*. Identical to the `get_current_user() is None`
  contract.
- **An authenticated user with no club** -- there is a viewer and they belong
  nowhere, so they see *nothing*. Failing open here would make "no club" the
  widest permission in the app, which is exactly backwards.

Managers therefore branch on the **user first**, and only then consult the club.

- **Membership is re-checked on every single request.** A club id in the
  session is an untrusted claim: someone removed from a club mid-session still
  holds its id, and a forged one costs nothing to try. The stored id may only
  *choose among* `Club.objects.for_user()`; it can never introduce a club.
  `set_active_club()` records a choice -- it is **not** an authorization check.
- **Staff do NOT bypass club scoping.** They keep the *user*-filter bypass, which
  is what makes admin work, but stay inside their clubs. Otherwise every staff
  page is a cross-tenant firehose and the isolation suite has to except its most
  privileged user. A staff member who needs to see a club joins it.
- **Resolution is lazy and memoized per request** (`_thread_locals.club`,
  `_thread_locals.user_clubs`). A page that never touches a scoped manager pays
  no query; a page that renders the club name and the switcher pays exactly one.
  `club_context` returns `SimpleLazyObject`s for the same reason.
- **The thread-locals are cleared on the way IN as well as out.** Clearing only
  on exit assumes every prior request left through the middleware; the one that
  did not -- a crash above it, a recycled worker, a test setting the value by
  hand -- would hand the next request a club resolved for somebody else.
- Two tabs in two clubs means last write wins. Accepted: a club is a viewing
  context, and per-tab state is not expressible in a session.

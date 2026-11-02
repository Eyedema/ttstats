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

The two boundaries (club, then user) are summarised in CLAUDE.md. Getting them
backwards, or conflating them, is the whole failure mode.

## Managers

- `CurrentUserMiddleware` stores the user in `_thread_locals.user`.
- **All six managers share `ScopedManagerMixin`.** Adding a boundary to six
  copies is how one gets missed, and a missed one leaks silently. Subclasses
  supply `club_lookup`, `_base_queryset()`, `_scope_to_user()`, and two knobs:
  - `filters_by_user = False` (PlayerManager, LiveMatchManager) skips the
    *participation* filter only. It must never skip the club filter.
  - `anonymous_returns_none = False` exists solely to keep the flag-off path
    byte-identical where the pre-tenancy manager served anonymous visitors.
- **Flag on for the whole suite fails ~200 tests by design**: those fixtures are
  a pre-backfill world (a user with no club sees nothing). The real claim --
  once a user has a club, the flag changes nothing they see -- is asserted by
  `test_tenancy_isolation.py::TestABackfilledWorldBehavesIdenticallyWithTheFlagOn`.
- **A `queryset=` in a form class body is evaluated at import time**, with no
  user and no club, so it is unfiltered forever -- a picker offering another
  club's players lets a user build a cross-club match. `ScopedChoicesMixin`
  re-binds them per instantiation.
- Which `all_objects` sites are scoped and which are transitively safe:
  `docs/decisions/kan-24-all-objects-audit.md`.

## New rows are stamped with the viewer's club

Scoping reads without stamping writes makes a newly created row invisible the
instant it is created (`club=NULL` never matches). `ClubStampedMixin` fills the
column in `save()` for `Player`, `Location`, `Match`, `ScheduledMatch` and
`Championship`.

- **At the model layer, not per view** -- a dozen creation paths exist (views,
  admin, `register_entry`, `generate_schedule`), and the per-view failure mode
  is the path somebody forgot. Isolation fixtures assign `club` explicitly, so
  only a test through a *creation path* catches this.
- **An explicit club always wins.** Stamping fills a blank; backfill and data
  fixes depend on that.
- **No viewer means no stamping; flag off means no stamping** -- same contracts
  as the managers, and it keeps stamping from fighting `backfill_clubs`.
- A null club on an existing row is filled on save. A row that already has one
  is **never** re-stamped, or an admin edit becomes a migration.

## The active club, and what `None` means

`get_current_club()` sits beside `get_current_user()` in `ttstats/middleware.py`.
`None` means two different things, and the difference is the security contract:

- **No user in context** (command, migration): no viewer, queries stay
  *unfiltered*.
- **Authenticated user with no club**: they see *nothing*. Failing open would
  make "no club" the widest permission in the app.

Managers therefore branch on the **user first**, then the club.

- **Membership is re-checked on every request.** A club id in the session is an
  untrusted claim; it may only *choose among* `Club.objects.for_user()`.
  `set_active_club()` records a choice; it is **not** an authorization check.
- **Staff do NOT bypass club scoping.** They keep the user-filter bypass only. A
  staff member who needs to see a club joins it.
- **Resolution is lazy and memoized per request** (`_thread_locals.club`,
  `_thread_locals.user_clubs`); `club_context` returns `SimpleLazyObject`s.
- **Thread-locals are cleared on the way IN as well as out**, so a request that
  never left through the middleware cannot hand its club to the next one.
- Two tabs in two clubs: last write wins. Accepted.

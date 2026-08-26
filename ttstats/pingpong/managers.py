"""Row-level security: the app's entire authorization surface.

Every manager here answers one question -- "what may the current viewer see?"
-- against two independent boundaries:

* **The club** (tenancy). Whose data is this? Flag-gated on ``clubs``.
* **The user** (participation). Which of my club's rows am I party to?

The two are *not* interchangeable and are applied in that order. Staff bypass
the user filter, which is what makes admin work; **nothing bypasses the club
filter**, because a staff account is global and a club is not (see
`CLAUDE.md` and KAN-14).

Before this file grew a mixin there were six near-duplicate implementations of
the same staff/anonymous/regular branch. Adding a second boundary to six copies
is how one gets missed, and a missed one is a cross-tenant leak that raises
nothing and logs nothing. `tests/test_tenancy_isolation.py` is the guard.
"""
import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import Exists, OuterRef, Q

logger = logging.getLogger(__name__)


class ScopedManagerMixin:
    """Shared club + user scoping. Compose with `models.Manager`.

    Subclasses supply the model-specific pieces:

    * ``club_lookup`` -- the ORM path from this model to a club.
    * ``_base_queryset()`` -- the unscoped queryset (e.g. excluding live
      matches). Override only when the model needs a standing filter.
    * ``_scope_to_user(qs, player)`` -- the participation predicate.
    * ``filters_by_user`` -- False for managers that deliberately skip the
      participation filter. It must **not** also skip the club filter.
    * ``anonymous_returns_none`` -- False only where the pre-tenancy manager
      served anonymous visitors, so the flag-off path stays byte-identical.
    """

    club_lookup = "club"
    filters_by_user = True
    anonymous_returns_none = True

    # -- hooks ---------------------------------------------------------------

    def _base_queryset(self):
        return super().get_queryset()

    def _scope_to_user(self, qs, player):
        """Restrict to rows `player` is party to. Override per model."""
        return qs

    def _queryset_for_user_without_player(self, qs):
        """A logged-in account with no Player profile.

        Defaults to nothing, which is the fail-closed answer.
        `ChampionshipManager` overrides it to preserve its documented
        public-championship behaviour.
        """
        return qs.none()

    # -- the boundary --------------------------------------------------------

    def _scope_to_club(self, qs, user):
        """Apply the tenancy filter. Returns None to mean "see nothing".

        None is distinct from an empty queryset here so the caller can decide;
        it is returned when there *is* a viewer and they belong to no club.
        """
        from pingpong import flags

        if not flags.is_enabled("clubs", user):
            return qs

        from ttstats.middleware import get_current_club

        club = get_current_club()
        if club is None:
            # A viewer who belongs nowhere sees nothing. Failing open here
            # would make "no club" the widest permission in the app.
            return None

        return qs.filter(**{self.club_lookup: club})

    def _canary(self, qs, user, source):
        """Report, never enforce. See `observability.check_queryset_scope`.

        Lives here rather than in six managers so there is one place to arm,
        disarm and reason about it -- which was half the point of the mixin.
        """
        from pingpong import observability

        if not observability.canary_enabled(user):
            return
        from ttstats.middleware import get_current_club

        observability.check_queryset_scope(
            qs, get_current_club(), self.club_lookup, source
        )

    def get_queryset(self):
        from ttstats.middleware import get_current_user

        qs = self._base_queryset()
        user = get_current_user()

        # No viewer at all -- a management command, a migration, a signal
        # outside a request. Unfiltered, and this contract predates tenancy.
        if not user:
            return qs

        # Club first, and it applies to staff too.
        scoped = self._scope_to_club(qs, user)
        if scoped is None:
            return qs.none()
        qs = scoped

        if not user.is_authenticated:
            return qs.none() if self.anonymous_returns_none else qs

        # Staff bypass the *user* filter. They have already been through the
        # club filter above and do not get out of it.
        if user.is_staff or user.is_superuser:
            return qs

        if not self.filters_by_user:
            self._canary(qs, user, f"{type(self).__name__}.get_queryset")
            return qs

        try:
            player = user.player
        except (AttributeError, ObjectDoesNotExist):
            return self._queryset_for_user_without_player(qs)

        scoped = self._scope_to_user(qs, player)
        self._canary(scoped, user, f"{type(self).__name__}.get_queryset")
        return scoped


class MatchManager(ScopedManagerMixin, models.Manager):
    """
    - Staff users: See all matches in their club
    - Regular users: See only matches they participated in
    - Anonymous users: See no matches

    Live matches (is_live=True) are excluded from every default query so they
    stay invisible to leaderboards, Elo, head-to-head, championships, etc.
    until the scorekeeper finishes them. Use ``Match.live_objects`` (or
    ``Match.all_objects``) to query live matches directly.
    """

    def _base_queryset(self):
        return super()._base_queryset().filter(is_live=False)

    def _scope_to_user(self, qs, player):
        from .models import Championship, MatchParticipant

        # Semi-joins rather than m2m joins: no row fan-out, so no
        # .distinct() and no ordering restrictions that come with it.
        mine = Exists(
            MatchParticipant.objects.filter(match_id=OuterRef('pk'), player=player)
        )
        championship_qs = Championship.all_objects.filter(
            pk=OuterRef('championship_id'),
            entry_members__player=player,
        )
        return qs.filter(mine | Exists(championship_qs))


class LiveMatchManager(ScopedManagerMixin, models.Manager):
    """Queryset over in-progress live matches only.

    Bypasses the row-level *user* filter so a scorekeeper can always look up
    their own live match by pk -- the view layer does the scorekeeper check.

    That bypass does **not** extend across clubs. A scorekeeper needing to
    reach their own match is not a reason to expose another club's, and the
    club filter is applied here exactly as it is everywhere else.
    """

    filters_by_user = False
    # Pre-tenancy this manager did no user check at all, so an anonymous
    # request saw live matches. Preserved for the flag-off path; with the flag
    # on, anonymous resolves to no club and therefore to nothing.
    anonymous_returns_none = False

    def _base_queryset(self):
        return super()._base_queryset().filter(is_live=True)


class PlayerManager(ScopedManagerMixin, models.Manager):
    """Players are visible to everyone **in the club** (read-only for non-staff).

    Before tenancy this returned every player in the database to every visitor
    -- the single widest exposure in the app, and the reason a second group
    could not be onboarded: a shared directory of strangers.

    There is still no *participation* filter. Within a club, seeing who else
    plays is the point.
    """

    filters_by_user = False
    # Preserved for the flag-off path only; see LiveMatchManager.
    anonymous_returns_none = False

    def editable_by(self, user):
        """Get players that user can edit.

        Unchanged by tenancy: it narrows `get_queryset()`, so it inherits the
        club scope without restating it, and a user can still only edit their
        own profile.
        """
        qs = self.get_queryset()

        if not user or not user.is_authenticated:
            return qs.none()

        if user.is_staff or user.is_superuser:
            return qs

        # Users can only edit their own player profile
        try:
            return qs.filter(user=user)
        except AttributeError:
            return qs.none()


class GameManager(ScopedManagerMixin, models.Manager):
    """
    Games are visible if their parent match is visible.
    Games of live matches are hidden until the match completes -- same
    rationale as MatchManager.

    Scoped through `match__club` rather than a column of its own: a game is
    always reachable via its match, and a second denormalized copy is a second
    thing that can disagree.
    """

    club_lookup = "match__club"

    def _base_queryset(self):
        return super()._base_queryset().filter(match__is_live=False)

    def _scope_to_user(self, qs, player):
        from .models import MatchParticipant

        return qs.filter(
            Exists(
                MatchParticipant.objects.filter(
                    match_id=OuterRef('match_id'), player=player
                )
            )
        )


class ChampionshipManager(ScopedManagerMixin, models.Manager):
    """
    Manager for championships.
    - Staff users: See all championships in their club
    - Regular users: See public championships + ones they participate in or created
    - Anonymous users: See no championships

    **`is_public` means open to anyone in this club** (epic Open Question 1,
    decided). Cross-club visibility is explicitly out of scope, and a public
    championship full of strangers is the shared-pool problem tenancy exists
    to remove.
    """

    def _scope_to_user(self, qs, player):
        from .models import ChampionshipEntryMember

        entered = Exists(
            ChampionshipEntryMember.objects.filter(
                championship_id=OuterRef('pk'), player=player
            )
        )
        return qs.filter(Q(is_public=True) | Q(created_by=player) | entered)

    def _queryset_for_user_without_player(self, qs):
        # Pre-tenancy behaviour, preserved: an account with no Player still
        # sees the public championships -- of its own club, now.
        return qs.filter(is_public=True)


class ScheduledMatchManager(ScopedManagerMixin, models.Manager):
    """
    Manager for scheduled matches.
    - Staff users: See all scheduled matches in their club
    - Regular users: See only scheduled matches they're participating in
    - Anonymous users: See no scheduled matches
    """

    def _scope_to_user(self, qs, player):
        from .models import Championship, ScheduledMatchParticipant

        mine = Exists(
            ScheduledMatchParticipant.objects.filter(
                scheduled_match_id=OuterRef('pk'), player=player
            )
        )
        championship_qs = Championship.all_objects.filter(
            pk=OuterRef('championship_id'),
            entry_members__player=player,
        )
        return qs.filter(mine | Exists(championship_qs))


# ---------------------------------------------------------------------------
# Explicit club scoping for the audited `all_objects` / `_base_manager` sites
# ---------------------------------------------------------------------------


def scope_to_active_club(queryset, club_lookup="club"):
    """Apply the club filter to a queryset that deliberately bypasses managers.

    `all_objects` and `_base_manager` exist to skip the *user* filter --
    championship views legitimately need every match in a championship, not
    just the viewer's. After tenancy they must not also skip the *club*
    filter, and there is no manager in the way to do it for them.

    Same contract as the managers: no viewer means unfiltered, a viewer with
    no club means nothing, and with the flag off it is a no-op.

    See KAN-24 for the audit of every call site.
    """
    from pingpong import flags
    from ttstats.middleware import get_current_club, get_current_user

    user = get_current_user()
    if not user:
        return queryset

    if not flags.is_enabled("clubs", user):
        return queryset

    club = get_current_club()
    if club is None:
        return queryset.none()

    scoped = queryset.filter(**{club_lookup: club})

    # These are the sites that bypass the managers on purpose, so they are
    # exactly where a scoping mistake is most likely and least visible.
    from pingpong import observability

    if observability.canary_enabled(user):
        observability.check_queryset_scope(
            scoped, club, club_lookup, "scope_to_active_club"
        )

    return scoped

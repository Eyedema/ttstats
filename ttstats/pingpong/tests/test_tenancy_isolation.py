"""Cross-tenant isolation (KAN-20 / Q1.11). THIS IS A SECURITY SUITE.

The tenancy boundary is the one thing in this epic that fails silently. A leak
does not raise, does not 500 and does not appear in any log -- a user simply
sees a name that is not theirs. Nothing else in the codebase catches that, so
this file is the acceptance criteria for S1.3 (KAN-15) and S1.5 (KAN-16), and
it was written before either of them.

Two properties make it worth keeping after the epic ships:

1. **It is driven by the URLconf, not a hand-written list.** `URL_EXPECTATIONS`
   must account for every name in the `pingpong` namespace, and
   `test_every_url_is_accounted_for` fails when a new view appears. A list that
   is not enforced rots, and a rotted isolation suite is worse than none
   because it reads like coverage.
2. **It asserts on response bodies, not status codes.** A 200 that renders the
   right shape with the wrong club's names is the failure that matters. Club B
   is populated with deliberately unmistakable strings so a substring search
   cannot accidentally pass.

Run standalone:
    cd ttstats && python -m pytest pingpong/tests/test_tenancy_isolation.py -q
"""
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from pingpong.models import Club, Match, Player, Side
from pingpong.services import set_match_sides
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import (
    ChampionshipFactory,
    ClubFactory,
    ClubMembershipFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
    confirm_match_silent,
)

# `override_settings` cannot decorate a plain pytest class, so the flag state
# is a fixture. Applied with `usefixtures` at class level, which also makes it
# impossible to write a test in one of these classes that forgets it.
CLUBS_ON = pytest.mark.usefixtures("clubs_on")
CLUBS_OFF = pytest.mark.usefixtures("clubs_off")


@pytest.fixture
def clubs_on(settings):
    settings.FEATURE_FLAGS = {"clubs": True}


@pytest.fixture
def clubs_off(settings):
    settings.FEATURE_FLAGS = {"clubs": False}

# Every Club B object carries this. If it appears in a response rendered for a
# Club A user, that is a leak -- and it is distinctive enough that a substring
# match cannot be a coincidence.
BRAVO = "ZzBravoSecret"


# ---------------------------------------------------------------------------
# The URL inventory. This table IS the deliverable.
# ---------------------------------------------------------------------------
#
# kind:
#   "detail"    -- takes a pk; a Club B pk must 404 for a Club A user
#   "list"      -- renders club data; must not contain BRAVO
#   "agnostic"  -- deliberately club-agnostic, with the reason stated
#
# Every entry marked "agnostic" needs a reason, because "agnostic" is how a
# leak gets waved through.

URL_EXPECTATIONS = {
    # --- Auth: pre-login or per-user, no club data on the page --------------
    "signup": ("agnostic", "Pre-login. The user has no club yet by definition."),
    "login": ("agnostic", "Pre-login."),
    "email_verify": ("agnostic", "Token-addressed, pre-login, renders no club data."),
    "email_resend_verification": ("agnostic", "Pre-login."),

    # --- Club administration (KAN-17) ---------------------------------------
    # Scoped to the viewer's *active* club, not to a pk in the URL, so there is
    # no cross-club pk to sweep. The membership ids in the two POST routes are
    # looked up through `self.club.memberships`, which makes a foreign id a 404
    # -- covered directly in test_club_roles.py.
    "club_settings": (
        "list",
        None,
    ),
    "club_member_role": (
        "agnostic",
        "Acts on the active club only; a foreign membership id 404s. "
        "Cross-club refusal is asserted in test_club_roles.py.",
    ),
    "club_member_remove": (
        "agnostic",
        "Same as club_member_role.",
    ),

    "club_switch": (
        "agnostic",
        "POST only, and it can only select from Club.objects.for_user() -- a "
        "forged club_id is refused. Covered in test_club_switcher.py.",
    ),

    # --- Club invites (KAN-18) ----------------------------------------------
    "club_invite_create": (
        "agnostic",
        "Issues an invite for the viewer's ACTIVE club only -- there is no "
        "club id in the request to forge. Admin-gating is in test_club_roles.py.",
    ),
    "club_invite_revoke": (
        "agnostic",
        "Looked up through `self.club.invites`, so another club's invite id "
        "404s. Asserted in test_club_invites.py.",
    ),
    "club_invite_accept": (
        "agnostic",
        "Token-addressed and deliberately reachable by anyone holding the "
        "link -- that is the feature. The token IS the authorization, it is "
        "rate limited, and it grants membership rather than exposing data.",
    ),

    # --- Per-user settings: scoped to request.user, never to a club ---------
    "passkey_management": (
        "agnostic",
        "Lists only request.user's own credentials. A club cannot appear.",
    ),
    "notification_settings": (
        "agnostic",
        "One NotificationPreference row, keyed on request.user.",
    ),
    "push_subscribe": ("agnostic", "Writes a PushSubscription for request.user only."),
    "push_unsubscribe": ("agnostic", "Deletes request.user's own subscription by endpoint."),
    "push_test": ("agnostic", "Sends to request.user's own devices."),

    # --- Creation forms: covered as lists, because their player pickers ------
    # would otherwise offer cross-club players and let a user build a
    # cross-club match through the UI.
    "player_add": ("agnostic", "A blank form. No existing player is listed."),
    "match_add": ("list", None),
    "match_schedule": ("list", None),
    "championship_create": ("list", None),

    # --- Listing and aggregate screens --------------------------------------
    "dashboard": ("list", None),
    "play": ("list", None),
    "leaderboard": ("list", None),
    "head_to_head": ("list", None),
    "calendar": ("list", None),
    "player_list": ("list", None),
    "match_list": ("list", None),
    "championship_list": ("list", None),

    # --- htmx fragments. Separate entry points, easy to miss in a view audit -
    "match_validate": ("list", None),
    "championship_participants_fragment": ("list", None),

    # --- Direct-pk detail routes --------------------------------------------
    "player_detail": ("detail", "player"),
    "player_edit": ("detail", "player"),
    "match_detail": ("detail", "match"),
    "match_edit": ("detail", "match"),
    "game_add": ("detail", "match"),
    "match_confirm": ("detail", "match"),
    "scheduled_match_detail": ("detail", "scheduled"),
    "scheduled_match_convert": ("detail", "scheduled"),
    "scheduled_match_edit": ("detail", "scheduled"),
    "championship_detail": ("detail", "championship"),
    "championship_edit": ("detail", "championship"),
    "championship_register": ("detail", "championship"),
    "championship_unregister": ("detail", "championship"),
    "championship_start": ("detail", "championship"),

    # --- The live scoreboard -------------------------------------------------
    # LiveMatchManager deliberately bypasses the user filter so a scorekeeper
    # can always reach their own match. That bypass must not extend across
    # clubs, so these are detail routes like any other.
    "live_scoreboard": ("detail", "live_match"),
    "live_start": ("detail", "live_match"),
    "live_point": ("detail", "live_match"),
    "live_state": ("detail", "live_match"),
    "live_side_switch": ("detail", "live_match"),
    "live_undo": ("detail", "live_match"),
}

# Which HTTP verb each route actually answers. A GET against a POST-only view
# returns 405 and would pass a naive "not 200" assertion while testing nothing.
POST_ONLY = {
    "match_confirm",
    "championship_register",
    "championship_unregister",
    "championship_start",
    "live_start",
    "live_point",
    "live_side_switch",
    "live_undo",
    "push_subscribe",
    "push_unsubscribe",
    "push_test",
    "match_validate",
}

# The kwarg name each detail route wants for its pk.
PK_KWARG = {
    "game_add": "match_pk",
    "scheduled_match_convert": "scheduled_match_pk",
}

# A *valid* body for the endpoints that validate their payload before they look
# the object up. With an empty body these return 400 without ever touching the
# match, so the sweep would be asserting on a shape check rather than on the
# tenancy boundary -- passing while testing nothing.
POST_BODIES = {
    "live_start": {"initial_server": "team1"},
    "live_point": {"side": "team1"},
}


def _pingpong_url_names():
    """Every url_name registered under the `pingpong` namespace."""
    resolver = get_resolver()
    names = set()

    def walk(patterns, namespace):
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, entry.namespace or namespace)
            elif isinstance(entry, URLPattern) and entry.name:
                if namespace == "pingpong":
                    names.add(entry.name)

    walk(resolver.url_patterns, None)
    return names


# ---------------------------------------------------------------------------
# Fixtures: two fully-populated clubs
# ---------------------------------------------------------------------------


class Tenant:
    """One club, its member, and one of everything the app can show."""

    def __init__(self, label, marker):
        self.club = ClubFactory(name=f"{marker}Club", slug=f"{marker.lower()}-club")

        self.user = UserFactory(username=f"{marker.lower()}-user")
        self.player = PlayerFactory(name=f"{marker}Player", user=self.user, club=self.club)
        ClubMembershipFactory(club=self.club, user=self.user)

        self.other_player = PlayerFactory(name=f"{marker}Rival", club=self.club)

        self.match = MatchFactory(player1=self.player, player2=self.other_player)
        GameFactory(match=self.match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=self.match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=self.match, game_number=3, team1_score=11, team2_score=9)
        self.match.refresh_from_db()
        confirm_match_silent(self.match)
        Match.all_objects.filter(pk=self.match.pk).update(club=self.club)
        self.match.refresh_from_db()

        self.scheduled = ScheduledMatchFactory(
            player1=self.player, player2=self.other_player
        )
        type(self.scheduled).all_objects.filter(pk=self.scheduled.pk).update(
            club=self.club
        )

        self.championship = ChampionshipFactory(
            name=f"{marker}Cup",
            with_entries=[[self.player], [self.other_player]],
            created_by=self.player,
        )
        type(self.championship).all_objects.filter(pk=self.championship.pk).update(
            club=self.club
        )

        # A live match, driven by this club's player as scorekeeper.
        self.live_match = MatchFactory(player1=self.player, player2=self.other_player)
        # The real initial state, not a hand-made stub: live_scoring reads
        # keys a stub would not have, and the sweep must exercise the actual
        # code path rather than crash before reaching the tenancy check.
        from pingpong import live_scoring

        Match.all_objects.filter(pk=self.live_match.pk).update(
            is_live=True,
            club=self.club,
            scorekeeper=self.player,
            live_state=live_scoring.initial_state(self.live_match.best_of),
        )
        self.live_match.refresh_from_db()

    def pk_for(self, kind):
        return {
            "player": self.other_player.pk,
            "match": self.match.pk,
            "scheduled": self.scheduled.pk,
            "championship": self.championship.pk,
            "live_match": self.live_match.pk,
        }[kind]

    def client(self):
        client = Client()
        client.force_login(self.user)
        return client


@pytest.fixture(autouse=True)
def _no_thread_local_leak():
    """Manager-level assertions manipulate the thread-local directly."""
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.fixture
def alpha(db):
    return Tenant("alpha", "Alpha")


@pytest.fixture
def bravo(db):
    return Tenant("bravo", BRAVO)


def _url(name, tenant):
    kind, arg = URL_EXPECTATIONS[name]
    if kind != "detail":
        return reverse(f"pingpong:{name}")
    kwarg = PK_KWARG.get(name, "pk")
    return reverse(f"pingpong:{name}", kwargs={kwarg: tenant.pk_for(arg)})


def _fetch(client, name, url):
    if name in POST_ONLY:
        import json

        body = json.dumps(POST_BODIES.get(name, {}))
        return client.post(url, data=body, content_type="application/json")
    return client.get(url)


DETAIL_URLS = sorted(n for n, (k, _) in URL_EXPECTATIONS.items() if k == "detail")
LIST_URLS = sorted(n for n, (k, _) in URL_EXPECTATIONS.items() if k == "list")


# ---------------------------------------------------------------------------
# The anti-rot guard
# ---------------------------------------------------------------------------


class TestTheSweepCannotRot:
    def test_every_url_is_accounted_for(self):
        """A new view must be classified here before it can ship.

        This is what makes the suite outlive the epic. Without it, someone adds
        a view in six months, the sweep silently does not cover it, and the
        file still reads like it covers everything.
        """
        registered = _pingpong_url_names()
        missing = registered - set(URL_EXPECTATIONS)
        assert not missing, (
            f"New pingpong URL(s) not classified in test_tenancy_isolation.py: "
            f"{sorted(missing)}. Add each to URL_EXPECTATIONS as 'detail', "
            f"'list', or 'agnostic' with a stated reason."
        )

    def test_the_table_has_no_stale_entries(self):
        registered = _pingpong_url_names()
        stale = set(URL_EXPECTATIONS) - registered
        assert not stale, f"URL_EXPECTATIONS names routes that no longer exist: {sorted(stale)}"

    def test_every_agnostic_entry_states_a_reason(self):
        """'Agnostic' is how a leak gets waved through, so it costs a sentence."""
        for name, (kind, note) in URL_EXPECTATIONS.items():
            if kind == "agnostic":
                assert note, f"{name} is marked club-agnostic with no reason given"


# ---------------------------------------------------------------------------
# Direct-pk access
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@CLUBS_ON
class TestDirectPkAccessIsRefused:
    @pytest.mark.parametrize("name", DETAIL_URLS)
    def test_club_b_object_by_direct_pk_is_404(self, name, alpha, bravo):
        """404, never 403 and never 200.

        404 is the existing documented convention for an invisible match, and
        it is the right one: 403 confirms the object exists, which is itself a
        disclosure across a tenancy boundary.
        """
        response = _fetch(alpha.client(), name, _url(name, bravo))
        assert response.status_code == 404, (
            f"{name} returned {response.status_code} for a cross-club pk"
        )

    @pytest.mark.parametrize("name", DETAIL_URLS)
    def test_the_same_url_works_for_its_own_club(self, name, alpha):
        """Guards against the suite passing because everything 404s."""
        response = _fetch(alpha.client(), name, _url(name, alpha))
        assert response.status_code != 404, (
            f"{name} 404s for its OWN club — the scoping is too tight"
        )

    def test_a_cross_club_404_does_not_echo_the_object(self, alpha, bravo):
        """Error pages must not leak the name they refused to show."""
        response = alpha.client().get(_url("match_detail", bravo))
        assert BRAVO not in response.content.decode(errors="ignore")


# ---------------------------------------------------------------------------
# Rendered bodies
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@CLUBS_ON
class TestNoClubBDataInAnyRenderedPage:
    @pytest.mark.parametrize("name", LIST_URLS)
    def test_no_club_b_marker_appears(self, name, alpha, bravo):
        """The failure that matters is a 200 with the wrong club's names."""
        response = _fetch(alpha.client(), name, _url(name, alpha))
        body = response.content.decode(errors="ignore")
        assert BRAVO not in body, (
            f"{name} leaked Club B data into a Club A user's page"
        )

    def test_the_marker_check_would_actually_catch_a_leak(self, bravo):
        """A control: Club B's own user must see the marker.

        Without this, every assertion above could be passing because the marker
        never renders anywhere, and the suite would be worthless.
        """
        response = bravo.client().get(reverse("pingpong:player_list"))
        assert BRAVO in response.content.decode(errors="ignore")

    def test_the_player_picker_does_not_offer_cross_club_players(self, alpha, bravo):
        """A picker that still offers them lets a user build a cross-club match
        through the UI, which is a leak with a write attached."""
        for name in ("match_add", "match_schedule"):
            response = alpha.client().get(reverse(f"pingpong:{name}"))
            assert BRAVO not in response.content.decode(errors="ignore"), name


# ---------------------------------------------------------------------------
# The cached leaderboard: correct queries served from another club's cache
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@CLUBS_ON
class TestLeaderboardCacheIsNotShared:
    """A correctly-scoped query served from a cache entry built for another
    club is still a leak -- and one every test running against a cold cache
    passes."""

    URL = None

    def _leaderboard(self, tenant, **params):
        return tenant.client().get(reverse("pingpong:leaderboard"), params)

    def test_club_b_first_then_club_a(self, alpha, bravo):
        self._leaderboard(bravo)  # warms the cache with Club B's payload
        body = self._leaderboard(alpha).content.decode(errors="ignore")
        assert BRAVO not in body

    def test_club_a_first_then_club_b(self, alpha, bravo):
        """Both orders, because a key collision is symmetric and testing one
        direction can pass by luck."""
        self._leaderboard(alpha)
        body = self._leaderboard(bravo).content.decode(errors="ignore")
        assert "AlphaPlayer" not in body and "AlphaRival" not in body

    def test_identical_filters_do_not_collide(self, alpha, bravo):
        params = {"match_type": "singles", "date_filter": "all", "top_x": "10"}
        self._leaderboard(bravo, **params)
        body = self._leaderboard(alpha, **params).content.decode(errors="ignore")
        assert BRAVO not in body

    def test_the_htmx_fragment_path_is_also_scoped(self, alpha, bravo):
        """LeaderboardView is dual-mode; the fragment is a separate entry
        point and renders from the same cache."""
        bravo.client().get(
            reverse("pingpong:leaderboard"), HTTP_HX_REQUEST="true"
        )
        response = alpha.client().get(
            reverse("pingpong:leaderboard"), HTTP_HX_REQUEST="true"
        )
        assert BRAVO not in response.content.decode(errors="ignore")

    def test_invalidating_club_a_does_not_bump_club_b(self, alpha, bravo):
        from pingpong import cache_utils

        self._leaderboard(bravo)
        before = cache.get(f"leaderboard_generation:{bravo.club.pk}")
        cache_utils.invalidate_leaderboard(club=alpha.club)
        after = cache.get(f"leaderboard_generation:{bravo.club.pk}")
        assert before == after


# ---------------------------------------------------------------------------
# Manager level -- below the views, where the boundary actually lives
# ---------------------------------------------------------------------------


def _as(user, club=None):
    """Put a user (and optionally a club) into thread-local context."""
    _thread_locals.user = user
    _thread_locals.session = {}
    if club is not None:
        _thread_locals.club = club


@pytest.mark.django_db
@CLUBS_ON
class TestManagersAreScoped:
    def test_player_manager_hides_other_clubs(self, alpha, bravo):
        """The widest current exposure: PlayerManager returns every player to
        everyone today."""
        _as(alpha.user, alpha.club)
        names = set(Player.objects.values_list("name", flat=True))
        assert bravo.player.name not in names
        assert alpha.player.name in names

    def test_match_manager_hides_other_clubs(self, alpha, bravo):
        _as(alpha.user, alpha.club)
        assert bravo.match not in Match.objects.all()

    def test_live_match_manager_hides_other_clubs(self, alpha, bravo):
        """LiveMatchManager bypasses the user filter for scorekeepers. That
        bypass must not extend across clubs."""
        _as(alpha.user, alpha.club)
        assert bravo.live_match not in Match.live_objects.all()

    def test_game_manager_hides_other_clubs(self, alpha, bravo):
        from pingpong.models import Game

        _as(alpha.user, alpha.club)
        bravo_games = set(
            Game.all_objects.filter(match=bravo.match).values_list("pk", flat=True)
        )
        visible = set(Game.objects.values_list("pk", flat=True))
        assert not (bravo_games & visible)

    def test_championship_manager_hides_other_clubs(self, alpha, bravo):
        from pingpong.models import Championship

        _as(alpha.user, alpha.club)
        assert bravo.championship not in Championship.objects.all()

    def test_scheduled_match_manager_hides_other_clubs(self, alpha, bravo):
        from pingpong.models import ScheduledMatch

        _as(alpha.user, alpha.club)
        assert bravo.scheduled not in ScheduledMatch.objects.all()

    def test_a_public_championship_does_not_cross_clubs(self, alpha, bravo):
        """Epic Open Question 1, decided: `is_public` means open to anyone
        *in this club*. Cross-club anything is out of the epic's scope, and a
        public championship full of strangers is the shared-pool problem the
        epic exists to remove."""
        from pingpong.models import Championship

        Championship.all_objects.filter(pk=bravo.championship.pk).update(
            is_public=True
        )
        _as(alpha.user, alpha.club)
        assert bravo.championship not in Championship.objects.all()

    def test_no_user_in_context_is_unfiltered(self, alpha, bravo):
        """A management command or migration. This is the existing
        `get_current_user() is None` contract and it must survive."""
        assert Match.objects.count() >= 2
        assert Player.objects.count() >= 4

    def test_an_authenticated_user_with_no_club_sees_nothing(self, alpha, bravo):
        """The other meaning of None. Failing open here would make "no club"
        the widest permission in the app."""
        stranger = UserFactory()
        PlayerFactory(user=stranger)
        _as(stranger)

        assert Match.objects.count() == 0
        assert Player.objects.count() == 0


@pytest.mark.django_db
@CLUBS_ON
class TestStaffAreScopedToo:
    """Decided in S1.4: staff keep the user-filter bypass, not a club bypass.

    This class is the reason that decision had to be explicit -- a suite that
    excepts its most privileged user is testing the wrong thing.
    """

    def test_staff_in_club_a_cannot_see_club_b_players(self, alpha, bravo):
        staff = UserFactory(is_staff=True)
        PlayerFactory(user=staff, club=alpha.club)
        ClubMembershipFactory(club=alpha.club, user=staff)
        _as(staff, alpha.club)

        assert bravo.player.name not in set(
            Player.objects.values_list("name", flat=True)
        )

    def test_staff_in_club_a_get_404_on_a_club_b_match(self, alpha, bravo):
        staff = UserFactory(is_staff=True, username="alpha-staff")
        PlayerFactory(user=staff, club=alpha.club)
        ClubMembershipFactory(club=alpha.club, user=staff)

        client = Client()
        client.force_login(staff)
        response = client.get(_url("match_detail", bravo))

        assert response.status_code == 404

    def test_a_superuser_with_no_club_sees_nothing(self, alpha, bravo):
        root = UserFactory(is_staff=True, is_superuser=True)
        PlayerFactory(user=root)
        _as(root)

        assert Player.objects.count() == 0


# ---------------------------------------------------------------------------
# Flag off: assert legacy behaviour explicitly. Never silently pass.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@CLUBS_OFF
class TestLegacyBehaviourWithTheFlagOff:
    """With `clubs` off, every query must be byte-identical to pre-tenancy.

    These are deliberately positive assertions rather than skips. A skipped
    suite reports green and proves nothing, and the flag-off path is what
    production runs until the cutover -- it is the *more* important of the two.
    """

    def test_players_are_visible_across_clubs(self, alpha, bravo):
        _as(alpha.user, alpha.club)
        names = set(Player.objects.values_list("name", flat=True))
        assert bravo.player.name in names, (
            "flag off must preserve the pre-tenancy behaviour exactly"
        )

    def test_a_user_still_sees_only_their_own_matches(self, alpha, bravo):
        """The *user* filter is unchanged by this epic and must keep working."""
        _as(alpha.user, alpha.club)
        assert alpha.match in Match.objects.all()
        assert bravo.match not in Match.objects.all()

    def test_a_user_with_no_club_is_not_locked_out(self, alpha):
        """Before the cutover, nobody has a club. Failing closed on the flag-off
        path would empty the app for every existing user."""
        stranger = UserFactory()
        PlayerFactory(user=stranger)
        _as(stranger)

        assert Player.objects.count() > 0

    def test_the_player_list_still_shows_everyone(self, alpha, bravo):
        response = alpha.client().get(reverse("pingpong:player_list"))
        assert BRAVO in response.content.decode(errors="ignore")


# ---------------------------------------------------------------------------
# What "green with the flag on" actually means
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestABackfilledWorldBehavesIdenticallyWithTheFlagOn:
    """The claim these tests exist to make precise.

    Running the *whole* suite with `clubs` forced on fails ~200 tests. That is
    not a defect: those fixtures build a pre-backfill world -- a logged-in user
    with no club -- and the designed answer there is "see nothing". Failing
    open instead would make "no club" the widest permission in the app.

    Production is never in that state once step 3 of the rollout runbook
    passes its gate, which is exactly why the gate asserts
    `memberships >= users`.

    So the honest claim is not "the old suite passes with the flag on" -- it
    cannot, without rewriting 35 pre-tenancy test files, which would contradict
    S1.1's promise that no existing test needed changing. The claim is this:
    **once a user has a club, the flag changes nothing they can see.** That is
    what a post-backfill production looks like, and it is what these assert.
    """

    def _observations(self, tenant):
        from pingpong.models import Championship, Game, ScheduledMatch

        _as(tenant.user, tenant.club)
        return {
            "players": set(Player.objects.values_list("pk", flat=True)),
            "matches": set(Match.objects.values_list("pk", flat=True)),
            "games": set(Game.objects.values_list("pk", flat=True)),
            "scheduled": set(ScheduledMatch.objects.values_list("pk", flat=True)),
            "championships": set(Championship.objects.values_list("pk", flat=True)),
        }

    def test_a_single_club_user_sees_the_same_rows_either_way(
        self, alpha, settings
    ):
        """The single-club case -- i.e. every current user, after the backfill.
        The default club should be invisible to them."""
        settings.FEATURE_FLAGS = {"clubs": False}
        without = self._observations(alpha)

        for attr in REQUEST_LOCAL_ATTRS:
            if hasattr(_thread_locals, attr):
                delattr(_thread_locals, attr)

        settings.FEATURE_FLAGS = {"clubs": True}
        with_flag = self._observations(alpha)

        assert with_flag == without

    def test_the_pages_a_backfilled_user_loads_are_unchanged(self, alpha, settings):
        settings.FEATURE_FLAGS = {"clubs": False}
        before = {
            name: alpha.client().get(_url(name, alpha)).status_code
            for name in LIST_URLS
        }

        settings.FEATURE_FLAGS = {"clubs": True}
        after = {
            name: alpha.client().get(_url(name, alpha)).status_code
            for name in LIST_URLS
        }

        assert after == before

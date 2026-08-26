"""Resolving the active club per request (KAN-14 / S1.4).

Two things are load-bearing here and both are easy to get quietly wrong:

1. **Membership is re-checked on every request.** A club id in a session is an
   untrusted claim. It may only *choose among* the clubs the user is in; it can
   never introduce one.
2. **The thread-local is cleared in a `finally`.** A leaked club is a
   cross-tenant read on whatever request the thread serves next.
"""
import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from pingpong.context_processors import club_context
from ttstats.middleware import (
    ACTIVE_CLUB_SESSION_KEY,
    CurrentUserMiddleware,
    _thread_locals,
    get_current_club,
    set_active_club,
)

from .conftest import ClubFactory, ClubMembershipFactory, PlayerFactory, UserFactory


@pytest.fixture(autouse=True)
def _clean_thread_locals():
    """Every test here manipulates the thread-local directly, so both ends of
    every test must be clean — the convention `test_managers.py` already uses."""
    for attr in ("user", "session", "club", "user_clubs"):
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in ("user", "session", "club", "user_clubs"):
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


def _request(user, session=None):
    request = RequestFactory().get("/")
    request.user = user
    request.session = {} if session is None else session
    return request


def _run(user, session=None, view=None):
    """Drive a request through the middleware, returning what the view saw."""
    seen = {}

    def inner(request):
        seen["club"] = get_current_club()
        if view is not None:
            view(request)
        return "response"

    CurrentUserMiddleware(inner)(_request(user, session))
    return seen.get("club")


@pytest.mark.django_db
class TestResolution:
    def test_a_user_in_exactly_one_club_gets_that_club(self):
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)

        assert _run(user) == club

    def test_a_user_in_no_clubs_gets_none(self):
        """A fresh signup, before they accept an invite. Every downstream
        surface has to handle this."""
        assert _run(UserFactory()) is None

    def test_an_anonymous_user_gets_none(self):
        assert _run(AnonymousUser()) is None

    def test_no_user_in_context_gets_none(self):
        """A management command or a migration. Downstream reads this as
        unfiltered, matching the get_current_user() contract."""
        assert get_current_club() is None

    def test_a_stored_selection_is_honoured(self):
        user = UserFactory()
        first = ClubFactory(name="Alpha")
        second = ClubFactory(name="Zulu")
        ClubMembershipFactory(club=first, user=user)
        ClubMembershipFactory(club=second, user=user)

        assert _run(user, {ACTIVE_CLUB_SESSION_KEY: second.pk}) == second

    def test_the_fallback_is_deterministic(self):
        """`for_user` orders by name, so the same user lands in the same club
        on every request until they choose otherwise."""
        user = UserFactory()
        zulu = ClubFactory(name="Zulu")
        alpha = ClubFactory(name="Alpha")
        ClubMembershipFactory(club=zulu, user=user)
        ClubMembershipFactory(club=alpha, user=user)

        assert _run(user) == alpha
        assert _run(user) == alpha


@pytest.mark.django_db
class TestMembershipIsRecheckedEveryRequest:
    """This check is the tenancy boundary."""

    def test_a_forged_club_id_is_rejected(self):
        user = UserFactory()
        mine = ClubFactory(name="Mine")
        theirs = ClubFactory(name="Theirs")
        ClubMembershipFactory(club=mine, user=user)
        ClubMembershipFactory(club=theirs, user=UserFactory())

        assert _run(user, {ACTIVE_CLUB_SESSION_KEY: theirs.pk}) == mine

    def test_a_club_id_for_a_club_that_does_not_exist_is_rejected(self):
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)

        assert _run(user, {ACTIVE_CLUB_SESSION_KEY: 999999}) == club

    def test_a_selection_the_user_has_since_left_falls_back(self):
        user = UserFactory()
        left = ClubFactory(name="Alpha")
        kept = ClubFactory(name="Bravo")
        membership = ClubMembershipFactory(club=left, user=user)
        ClubMembershipFactory(club=kept, user=user)
        membership.delete()

        assert _run(user, {ACTIVE_CLUB_SESSION_KEY: left.pk}) == kept

    def test_a_rejected_selection_is_discarded_from_the_session(self):
        """Otherwise the stale id keeps failing the check on every subsequent
        request, forever."""
        user = UserFactory()
        mine, theirs = ClubFactory(name="Mine"), ClubFactory(name="Theirs")
        ClubMembershipFactory(club=mine, user=user)
        session = {ACTIVE_CLUB_SESSION_KEY: theirs.pk}

        _run(user, session)

        assert ACTIVE_CLUB_SESSION_KEY not in session

    def test_a_user_who_left_their_only_club_gets_none_not_someone_elses(self):
        user = UserFactory()
        theirs = ClubFactory(name="Theirs")
        ClubMembershipFactory(club=theirs, user=UserFactory())

        assert _run(user, {ACTIVE_CLUB_SESSION_KEY: theirs.pk}) is None


@pytest.mark.django_db
class TestStaffDoesNotBypassClubScoping:
    """Decided explicitly (epic Open Question / S1.4 edge case).

    Staff keeps its *user*-filter bypass — that is what makes admin work — but
    stays club-scoped. Otherwise every staff page is a cross-tenant firehose,
    and the isolation suite would have to except its most privileged user,
    which is the one it should test hardest.

    A staff member who needs to see a club joins it.
    """

    def test_staff_with_no_membership_gets_no_club(self):
        staff = UserFactory(is_staff=True)
        ClubFactory()

        assert _run(staff) is None

    def test_a_superuser_cannot_select_a_club_they_are_not_in(self):
        superuser = UserFactory(is_staff=True, is_superuser=True)
        theirs = ClubFactory(name="Theirs")
        ClubMembershipFactory(club=theirs, user=UserFactory())

        assert _run(superuser, {ACTIVE_CLUB_SESSION_KEY: theirs.pk}) is None

    def test_staff_who_are_members_get_their_club(self):
        staff = UserFactory(is_staff=True)
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=staff)

        assert _run(staff) == club


@pytest.mark.django_db
class TestThreadLocalHygiene:
    def test_club_state_does_not_leak_to_the_next_request(self):
        user_a = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user_a)

        assert _run(user_a) == club
        assert not hasattr(_thread_locals, "club")
        assert _run(UserFactory()) is None

    def test_club_state_is_cleared_when_the_view_raises(self):
        """A `finally`, not a happy-path cleanup: a leaked club is a
        cross-tenant read on whatever this thread serves next."""
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)

        def boom(request):
            get_current_club()
            raise ValueError("boom")

        with pytest.raises(ValueError):
            CurrentUserMiddleware(boom)(_request(user))

        assert not hasattr(_thread_locals, "club")
        assert not hasattr(_thread_locals, "user")
        assert not hasattr(_thread_locals, "session")

    def test_resolution_is_memoized_within_a_request(
        self, django_assert_num_queries
    ):
        """This runs on every request including htmx fragments."""
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)

        def inner(request):
            with django_assert_num_queries(1):
                get_current_club()
                get_current_club()
                get_current_club()
            return "response"

        CurrentUserMiddleware(inner)(_request(user))

    def test_an_anonymous_request_costs_no_query(self, django_assert_num_queries):
        def inner(request):
            with django_assert_num_queries(0):
                get_current_club()
            return "response"

        CurrentUserMiddleware(inner)(_request(AnonymousUser()))

    def test_resolution_is_lazy(self, django_assert_num_queries):
        """A page that never touches a scoped manager must pay nothing."""
        user = UserFactory()
        ClubMembershipFactory(user=user)

        def inner(request):
            return "response"

        with django_assert_num_queries(0):
            CurrentUserMiddleware(inner)(_request(user))


@pytest.mark.django_db
class TestSetActiveClub:
    def test_it_records_the_choice_in_the_session(self):
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)
        request = _request(user)

        set_active_club(request, club)

        assert request.session[ACTIVE_CLUB_SESSION_KEY] == club.pk

    def test_the_choice_takes_effect_immediately(self):
        """Without this the switch only lands on the *next* request, and the
        page the user is redirected to still shows the old club."""
        user = UserFactory()
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        ClubMembershipFactory(club=alpha, user=user)
        ClubMembershipFactory(club=zulu, user=user)

        _thread_locals.user = user
        _thread_locals.session = {}
        assert get_current_club() == alpha

        set_active_club(_request(user), zulu)
        assert get_current_club() == zulu

    def test_the_recorded_choice_is_still_re_checked_next_request(self):
        """set_active_club records a choice. It is not an authorization check."""
        user = UserFactory()
        theirs = ClubFactory(name="Theirs")
        mine = ClubFactory(name="Mine")
        ClubMembershipFactory(club=mine, user=user)
        session = {}

        set_active_club(_request(user, session), theirs)
        assert session[ACTIVE_CLUB_SESSION_KEY] == theirs.pk

        assert _run(user, session) == mine


@pytest.mark.django_db
class TestClubContextProcessor:
    def test_anonymous_gets_no_club(self):
        context = club_context(_request(AnonymousUser()))
        assert context == {
            "active_club": None,
            "user_clubs": [],
            "has_multiple_clubs": False,
        }

    def test_a_single_club_user_gets_their_club_and_no_switcher(self):
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)

        _thread_locals.user = user
        _thread_locals.session = {}
        context = club_context(_request(user))

        # `==` not `is`: the values are SimpleLazyObjects so the query only
        # fires if a template actually reads them.
        assert context["active_club"] == club
        assert list(context["user_clubs"]) == [club]
        assert bool(context["has_multiple_clubs"]) is False

    def test_a_multi_club_user_gets_the_switcher_flag(self):
        user = UserFactory()
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        ClubMembershipFactory(club=alpha, user=user)
        ClubMembershipFactory(club=zulu, user=user)

        _thread_locals.user = user
        _thread_locals.session = {}
        context = club_context(_request(user))

        assert bool(context["has_multiple_clubs"]) is True
        assert list(context["user_clubs"]) == [alpha, zulu]

    def test_the_context_processor_costs_no_query_until_it_is_read(
        self, django_assert_num_queries
    ):
        """It runs on every render. Until the S1.8 switcher exists no template
        reads any of it, so resolving eagerly would add a query to every page
        in the app in exchange for nothing."""
        user = UserFactory()
        ClubMembershipFactory(user=user)

        _thread_locals.user = user
        _thread_locals.session = {}
        with django_assert_num_queries(0):
            club_context(_request(user))

    def test_reading_the_club_and_the_list_costs_one_query_between_them(
        self, django_assert_num_queries
    ):
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)

        _thread_locals.user = user
        _thread_locals.session = {}
        context = club_context(_request(user))
        with django_assert_num_queries(1):
            assert context["active_club"] == club
            assert list(context["user_clubs"]) == [club]
            assert bool(context["has_multiple_clubs"]) is False

    def test_a_user_with_no_player_still_gets_club_context(self):
        """`pingpong_context` early-returns for a user with no Player. This
        must not, or a half-set-up account loses the club name from its chrome
        on exactly the pages it sees."""
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)
        assert not hasattr(user, "player")

        _thread_locals.user = user
        _thread_locals.session = {}
        context = club_context(_request(user))

        assert context["active_club"] == club

    def test_a_user_with_no_clubs_gets_none_not_a_crash(self):
        user = UserFactory()
        PlayerFactory(user=user)

        _thread_locals.user = user
        _thread_locals.session = {}
        context = club_context(_request(user))

        assert context["active_club"] == None  # noqa: E711 -- lazy, so not `is`
        assert bool(context["has_multiple_clubs"]) is False

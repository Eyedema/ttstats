"""The club switcher in the navigation spine (KAN-19 / S1.8).

pytest can prove the switcher is in the markup and that switching works. It
cannot prove the control is on screen, tappable, or legible at 320px -- the
rendered string was perfect while the mobile drawer was shipping permanently
open on iOS Safari. `tests/e2e/club-switcher.spec.js` covers that half, and it
is not optional here.
"""
import pytest
from django.test import Client
from django.urls import reverse

from pingpong.models import ClubRole
from ttstats.middleware import (
    ACTIVE_CLUB_SESSION_KEY,
    REQUEST_LOCAL_ATTRS,
    _thread_locals,
)

from .conftest import ClubFactory, ClubMembershipFactory, PlayerFactory, UserFactory

DASHBOARD = reverse("pingpong:dashboard")
SWITCH = reverse("pingpong:club_switch")


@pytest.fixture(autouse=True)
def _clean_context():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.fixture(autouse=True)
def clubs_on(settings):
    settings.FEATURE_FLAGS = {"clubs": True, "tenancy_canary": False}


def _member(club, role=ClubRole.MEMBER, user=None):
    user = user or UserFactory()
    if not hasattr(user, "player"):
        PlayerFactory(user=user, club=club)
    ClubMembershipFactory(club=club, user=user, role=role)
    return user


def _client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestSingleClubUsersSeeNoSwitcher:
    """A chooser offered to someone with one choice is clutter."""

    def test_no_switching_control_is_rendered(self):
        club = ClubFactory(name="Sunday Bashers")
        user = _member(club)

        body = _client(user).get(DASHBOARD).content.decode()

        assert "data-club-switcher" not in body
        assert SWITCH not in body

    def test_the_club_name_is_still_shown(self):
        """"Which club am I looking at" must be answerable on every screen,
        switcher or no switcher."""
        club = ClubFactory(name="Sunday Bashers")
        user = _member(club)

        body = _client(user).get(DASHBOARD).content.decode()

        assert "Sunday Bashers" in body
        assert "data-club-name" in body


@pytest.mark.django_db
class TestMultiClubUsersGetASwitcher:
    def test_every_club_is_listed(self):
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)

        body = _client(user).get(DASHBOARD).content.decode()

        assert "data-club-switcher" in body
        assert "Alpha" in body and "Zulu" in body

    def test_the_active_club_is_marked(self):
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)

        body = _client(user).get(DASHBOARD).content.decode()

        # Marked with aria-current, not with colour alone -- colour is never
        # the only signal anywhere in this design.
        assert 'aria-current="true"' in body

    def test_it_is_a_post_not_a_link(self):
        """Switching changes what every later page shows. A GET would let any
        page on the internet move someone's club with an <img> tag."""
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)

        body = _client(user).get(DASHBOARD).content.decode()

        assert f'action="{SWITCH}"' in body
        assert f'href="{SWITCH}"' not in body

    def test_a_get_to_the_switch_endpoint_is_refused(self):
        club = ClubFactory()
        user = _member(club)

        assert _client(user).get(SWITCH).status_code == 405


@pytest.mark.django_db
class TestSwitching:
    def test_it_changes_the_active_club(self):
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)
        client = _client(user)

        client.post(SWITCH, {"club_id": zulu.pk})

        assert client.session[ACTIVE_CLUB_SESSION_KEY] == zulu.pk

    def test_it_lands_on_today(self):
        """Staying put would leave the viewer on a detail page belonging to the
        club they just left, which then 404s."""
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)

        response = _client(user).post(SWITCH, {"club_id": zulu.pk})

        assert response.status_code == 302
        assert response["Location"] == DASHBOARD

    def test_subsequent_pages_are_scoped_to_the_new_club(self):
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)
        PlayerFactory(name="AlphaOnlyPlayer", club=alpha)
        PlayerFactory(name="ZuluOnlyPlayer", club=zulu)
        client = _client(user)

        client.post(SWITCH, {"club_id": zulu.pk})
        body = client.get(reverse("pingpong:player_list")).content.decode()

        assert "ZuluOnlyPlayer" in body
        assert "AlphaOnlyPlayer" not in body

    def test_switching_to_a_club_you_are_not_in_is_refused(self):
        """The switch view can only select from `Club.objects.for_user()`, and
        `_resolve_active_club` re-checks on every later request anyway."""
        mine, theirs = ClubFactory(name="Mine"), ClubFactory(name="Theirs")
        user = _member(mine)
        _member(theirs)
        client = _client(user)

        response = client.post(SWITCH, {"club_id": theirs.pk}, follow=True)

        assert "not a member" in response.content.decode().lower()
        assert client.session.get(ACTIVE_CLUB_SESSION_KEY) != theirs.pk

    def test_a_nonsense_club_id_does_not_500(self):
        club = ClubFactory()
        user = _member(club)

        response = _client(user).post(SWITCH, {"club_id": "not-a-number"})

        assert response.status_code == 302

    def test_a_missing_club_id_does_not_500(self):
        club = ClubFactory()
        user = _member(club)

        assert _client(user).post(SWITCH, {}).status_code == 302

    def test_an_anonymous_visitor_cannot_switch(self):
        club = ClubFactory()
        response = Client().post(SWITCH, {"club_id": club.pk})
        assert response.status_code in (302, 403)


@pytest.mark.django_db
class TestClubSettingsRow:
    """The row is offered only where it would work — a 403 is the backstop."""

    def test_an_admin_sees_it(self):
        club = ClubFactory()
        user = _member(club, ClubRole.ADMIN)

        body = _client(user).get(DASHBOARD).content.decode()

        assert reverse("pingpong:club_settings") in body

    def test_a_plain_member_does_not(self):
        club = ClubFactory()
        user = _member(club, ClubRole.MEMBER)

        body = _client(user).get(DASHBOARD).content.decode()

        assert reverse("pingpong:club_settings") not in body


@pytest.mark.django_db
class TestEdgeCases:
    def test_a_user_with_no_clubs_gets_no_club_block_and_no_crash(self):
        """A fresh signup, before they accept an invite."""
        user = UserFactory()
        PlayerFactory(user=user)

        response = _client(user).get(DASHBOARD)

        assert response.status_code == 200
        assert "data-club-switcher" not in response.content.decode()

    def test_a_user_removed_from_their_active_club_still_gets_a_page(self):
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)
        client = _client(user)
        client.post(SWITCH, {"club_id": zulu.pk})

        zulu.memberships.filter(user=user).delete()

        response = client.get(DASHBOARD)
        assert response.status_code == 200
        assert "Alpha" in response.content.decode()

    def test_a_very_long_club_name_is_truncated_not_wrapped(self):
        """320px is the narrowest supported width. The visual proof is the e2e
        spec; this asserts the mechanism is present at all."""
        club = ClubFactory(name="The Extremely Long Sunday Afternoon Bashers Society")
        user = _member(club)

        body = _client(user).get(DASHBOARD).content.decode()

        assert "truncate" in body
        assert club.name in body

    def test_switching_does_not_disturb_a_live_match_in_the_old_club(self):
        """Decided: switching is a *viewing* change. A live match is server
        state keyed on its own pk, and the scorekeeper reaches it by URL --
        so it keeps running and is waiting when they switch back."""
        from pingpong.models import Match

        from .conftest import MatchFactory

        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)
        scorekeeper = user.player
        opponent = PlayerFactory(club=alpha)
        match = MatchFactory(player1=scorekeeper, player2=opponent)
        Match.all_objects.filter(pk=match.pk).update(
            is_live=True, club=alpha, scorekeeper=scorekeeper
        )

        client = _client(user)
        client.post(SWITCH, {"club_id": zulu.pk})

        match.refresh_from_db()
        assert match.is_live is True

        # And it is reachable again the moment they switch back.
        client.post(SWITCH, {"club_id": alpha.pk})
        assert (
            client.get(
                reverse("pingpong:live_scoreboard", args=[match.pk])
            ).status_code
            == 200
        )


@pytest.mark.django_db
class TestTheNavPartialIsRenderedOnce:
    def test_the_drawer_and_the_sidebar_render_the_same_partial(self):
        """They were two hand-maintained copies once and had already diverged.
        Two occurrences of the switcher marker means both include the partial;
        one would mean somebody hardcoded a copy again."""
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        user = _member(alpha)
        _member(zulu, user=user)

        body = _client(user).get(DASHBOARD).content.decode()

        assert body.count("data-club-switcher") == 2

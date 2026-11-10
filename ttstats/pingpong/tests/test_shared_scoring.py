"""Shared live scoring: the host's lobby and the join flow (KAN-151, KAN-152).

The host starts a live match with no opponent, shows a QR code of
``/pingpong/live/join/<token>/``, and whoever opens it (signed in, in the
club) becomes side 2. The join token is a bearer credential and follows the
club-invite rules. ``TestSharedScoringIntegration`` is the required flow.
"""
import json
import logging
import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pingpong.live_lobby_views import PENDING_LIVE_JOIN_SESSION_KEY, find_lobby
from pingpong.models import (
    LOBBY_TTL,
    ClubInvite,
    LiveLobby,
    LiveScorer,
    Match,
    MatchConfirmation,
    Side,
)
from pingpong.services import cancel_lobby, open_lobby, start_shared_match
from pingpong.templatetags.qr_tags import path_data, qr_matrix
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    LiveLobbyFactory,
    LocationFactory,
    PlayerFactory,
    UserFactory,
)

FLAGS_ON = {"clubs": True, "shared_scoring": True, "tenancy_canary": False}

SIGNUP = {
    "username": "joiner",
    "email": "joiner@example.com",
    "full_name": "Marco Joiner",
    "playing_style": "normal",
    "password1": "a-very-long-passphrase-42",
    "password2": "a-very-long-passphrase-42",
}


@pytest.fixture(autouse=True)
def _clean_context():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.fixture
def flags_on(settings):
    settings.FEATURE_FLAGS = dict(FLAGS_ON)


@pytest.fixture
def club(db):
    return ClubFactory(name="Thursday Club")


def _member(club, **kwargs):
    """A verified member of ``club`` with a Player in it."""
    player = PlayerFactory(with_user=True, club=club, **kwargs)
    ClubMembershipFactory(club=club, user=player.user)
    player.user.profile.email_verified = True
    player.user.profile.save()
    return player


def _client(player=None):
    c = Client()
    if player is not None:
        c.force_login(player.user)
    return c


def _join_url(lobby):
    return reverse("pingpong:live_join", args=[lobby.token])


def _lobby_url(lobby):
    return reverse("pingpong:live_lobby", args=[lobby.pk])


def _status(client, lobby):
    return client.get(reverse("pingpong:live_lobby_status", args=[lobby.pk]))


def _post(client, name, match, body):
    return client.post(
        reverse(f"pingpong:{name}", args=[match.pk]),
        data=json.dumps(body),
        content_type="application/json",
    )


def _sync(client, match, side, seqs):
    ops = [{"seq": s, "type": "point", "side": side} for s in seqs]
    return _post(client, "live_sync", match, {"ops": ops})


# ---------------------------------------------------------------------------
# Model and manager (KAN-162)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLiveLobbyModel:
    def test_token_and_expiry_are_set_on_save(self):
        lobby = LiveLobbyFactory()
        assert re.fullmatch(r"[0-9a-f]{32}", lobby.token)
        assert lobby.expires_at - lobby.created_at == LOBBY_TTL
        assert LOBBY_TTL == timedelta(minutes=30)
        assert lobby.status == LiveLobby.Status.OPEN
        assert lobby.is_open

    def test_expiry_is_lazy(self):
        lobby = LiveLobbyFactory()
        LiveLobby.all_objects.filter(pk=lobby.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        lobby.refresh_from_db()
        assert lobby.status == LiveLobby.Status.OPEN  # nothing flipped it
        assert lobby.current_status == LiveLobby.Status.EXPIRED
        assert not lobby.is_open
        assert "expired" in lobby.ended_reason

    def test_a_closed_lobby_keeps_its_status(self):
        lobby = LiveLobbyFactory(status=LiveLobby.Status.CANCELLED)
        assert lobby.current_status == LiveLobby.Status.CANCELLED
        assert "cancelled" in lobby.ended_reason

    def test_the_database_allows_one_open_lobby_per_host(self):
        lobby = LiveLobbyFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            LiveLobbyFactory(host=lobby.host)

    def test_closed_lobbies_do_not_count(self):
        lobby = LiveLobbyFactory(status=LiveLobby.Status.CANCELLED)
        LiveLobbyFactory(host=lobby.host)  # no error


@pytest.mark.django_db
class TestLiveLobbyManager:
    def _as(self, user):
        _thread_locals.user = user

    def test_a_host_sees_only_their_own(self):
        mine, theirs = LiveLobbyFactory(), LiveLobbyFactory()
        self._as(mine.host.user)
        assert list(LiveLobby.objects.all()) == [mine]
        assert theirs in LiveLobby.all_objects.all()

    def test_anonymous_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        LiveLobbyFactory()
        self._as(AnonymousUser())
        assert not LiveLobby.objects.exists()

    def test_another_clubs_lobby_is_invisible_even_to_staff(self, flags_on, club):
        other = ClubFactory()
        LiveLobbyFactory(club=other, host=_member(other))
        staff = _member(club)
        staff.user.is_staff = True
        staff.user.save()
        mine = LiveLobbyFactory(club=club, host=_member(club))
        self._as(staff.user)
        assert list(LiveLobby.objects.all()) == [mine]

    def test_the_club_is_stamped_from_the_viewer(self, flags_on, club):
        host = _member(club)
        self._as(host.user)
        lobby = open_lobby(host, best_of=5)
        assert lobby.club == club


@pytest.mark.django_db
class TestOpenAndCancelLobby:
    def test_a_second_lobby_cancels_the_first(self):
        first = open_lobby(PlayerFactory(with_user=True), best_of=5)
        second = open_lobby(first.host, best_of=3)
        first.refresh_from_db()
        assert first.status == LiveLobby.Status.CANCELLED
        assert second.is_open and second.best_of == 3

    def test_an_expired_open_lobby_is_closed_as_expired(self):
        first = open_lobby(PlayerFactory(with_user=True), best_of=5)
        LiveLobby.all_objects.filter(pk=first.pk).update(expires_at=timezone.now())
        open_lobby(first.host, best_of=5)
        first.refresh_from_db()
        assert first.status == LiveLobby.Status.EXPIRED

    def test_cancel_is_conditional(self):
        lobby = LiveLobbyFactory(status=LiveLobby.Status.JOINED)
        assert cancel_lobby(lobby) is False
        lobby.refresh_from_db()
        assert lobby.status == LiveLobby.Status.JOINED
        open_one = LiveLobbyFactory()
        assert cancel_lobby(open_one) is True


# ---------------------------------------------------------------------------
# Entry point: the flag, the button, the create view
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateEntryPoint:
    def test_flag_is_declared_and_off_by_default(self):
        from django.conf import settings as dj

        from pingpong import flags

        assert "shared_scoring" in dj.FEATURE_FLAGS
        assert flags.is_enabled("shared_scoring") is False

    def test_flag_off_the_button_is_absent(self, settings):
        settings.FEATURE_FLAGS = {"clubs": False, "shared_scoring": False}
        resp = _client(PlayerFactory(with_user=True)).get(reverse("pingpong:match_add"))
        html = resp.content.decode()
        assert "Score live with a shared link" not in html
        assert reverse("pingpong:live_lobby_create") not in html

    def test_flag_off_create_is_404(self, settings):
        settings.FEATURE_FLAGS = {"clubs": False, "shared_scoring": False}
        resp = _client(PlayerFactory(with_user=True)).post(
            reverse("pingpong:live_lobby_create"), {"best_of": 5}
        )
        assert resp.status_code == 404
        assert not LiveLobby.all_objects.exists()

    def test_flag_on_the_button_posts_to_the_create_view(self, flags_on, club):
        resp = _client(_member(club)).get(reverse("pingpong:match_add"))
        html = resp.content.decode()
        assert "Score live with a shared link" in html
        assert f'formaction="{reverse("pingpong:live_lobby_create")}"' in html
        # Singles only: the doubles toggle hides it.
        assert re.search(r'formaction="[^"]+"[^>]*formnovalidate', html)

    def test_create_redirects_to_the_lobby(self, flags_on, club):
        host = _member(club)
        location = LocationFactory(club=club)
        resp = _client(host).post(
            reverse("pingpong:live_lobby_create"),
            {"best_of": 3, "location": location.pk, "is_double": "False", "player2": ""},
        )
        lobby = LiveLobby.all_objects.get()
        assert resp.status_code == 302
        assert resp.url == _lobby_url(lobby)
        assert (lobby.host, lobby.best_of, lobby.location, lobby.club) == (host, 3, location, club)

    def test_doubles_is_refused(self, flags_on, club):
        resp = _client(_member(club)).post(
            reverse("pingpong:live_lobby_create"), {"best_of": 5, "is_double": "True"}
        )
        assert resp.status_code == 302
        assert not LiveLobby.all_objects.exists()

    def test_an_illegal_best_of_is_refused(self, flags_on, club):
        _client(_member(club)).post(reverse("pingpong:live_lobby_create"), {"best_of": 4})
        assert not LiveLobby.all_objects.exists()

    def test_another_clubs_location_is_refused(self, flags_on, club):
        foreign = LocationFactory(club=ClubFactory())
        _client(_member(club)).post(
            reverse("pingpong:live_lobby_create"), {"best_of": 5, "location": foreign.pk}
        )
        assert not LiveLobby.all_objects.exists()

    def test_get_is_not_allowed(self, flags_on, club):
        resp = _client(_member(club)).get(reverse("pingpong:live_lobby_create"))
        assert resp.status_code == 405

    def test_anonymous_is_sent_to_login(self, flags_on):
        resp = Client().post(reverse("pingpong:live_lobby_create"), {"best_of": 5})
        assert resp.status_code == 302 and "login" in resp.url


# ---------------------------------------------------------------------------
# Lobby page and status endpoint (KAN-164)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLobbyPage:
    def test_shows_a_qr_code_of_the_absolute_join_url(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        resp = _client(host).get(_lobby_url(lobby))
        html = resp.content.decode()
        join_url = "http://testserver" + _join_url(lobby)
        svg = re.search(r"<svg[^>]*class=\"qr[^\"]*\".*?</svg>", html, re.S).group(0)
        # The encoded payload IS the join URL: same module matrix, same path.
        assert f'd="{path_data(qr_matrix(join_url))}"' in svg
        assert "style=" not in svg
        assert 'role="img"' in svg and "aria-label=" in svg
        # The URL as plain selectable text, for scripting off.
        assert f'data-testid="lobby-url">{join_url}</p>' in html

    def test_page_furniture(self, flags_on, club):
        host = _member(club)
        location = LocationFactory(club=club, name="Basement")
        lobby = LiveLobbyFactory(host=host, club=club, best_of=7, location=location)
        html = _client(host).get(_lobby_url(lobby)).content.decode()
        assert "Thursday Club" in html
        assert "Best of 7" in html and "Basement" in html
        local = timezone.localtime(lobby.expires_at).strftime("%H:%M")
        assert f"Expires at <span class=\"n\">{local}</span>" in html
        assert "Check again" in html
        assert "Pick opponent instead" in html and "Cancel" in html

    def test_share_and_copy_start_hidden_and_js_is_static(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        html = _client(host).get(_lobby_url(lobby)).content.decode()
        assert re.search(r"data-lobby-share hidden", html)
        assert re.search(r"data-lobby-copy hidden", html)
        assert 'id="live-lobby-config"' in html
        assert re.search(r'<script defer src="[^"]*live_lobby[^"]*\.js"', html)
        config = json.loads(
            re.search(r'id="live-lobby-config"[^>]*>(.*?)</script>', html, re.S).group(1)
        )
        assert config["status_url"] == reverse("pingpong:live_lobby_status", args=[lobby.pk])
        assert config["interval_ms"] == 2000

    def test_no_store(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        for resp in (_client(host).get(_lobby_url(lobby)), _status(_client(host), lobby)):
            assert "no-store" in resp["Cache-Control"]

    def test_another_player_gets_404(self, flags_on, club):
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        other = _client(_member(club))
        assert other.get(_lobby_url(lobby)).status_code == 404
        assert _status(other, lobby).status_code == 404

    def test_staff_in_the_club_get_404_too(self, flags_on, club):
        """Staff bypass the user filter, but the page shows a bearer token."""
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        staff = _member(club)
        staff.user.is_staff = True
        staff.user.save()
        assert _client(staff).get(_lobby_url(lobby)).status_code == 404

    def test_a_joined_lobby_redirects_to_the_scoreboard(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        match = start_shared_match(lobby, _member(club), _FakeRequest())
        resp = _client(host).get(_lobby_url(lobby))
        assert resp.status_code == 302
        assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])

    def test_an_expired_lobby_shows_why(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        LiveLobby.all_objects.filter(pk=lobby.pk).update(expires_at=timezone.now())
        html = _client(host).get(_lobby_url(lobby)).content.decode()
        assert "This code has expired." in html
        assert 'class="qr' not in html
        assert lobby.token not in html


@pytest.mark.django_db
class TestLobbyStatus:
    def test_open(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        assert _status(_client(host), lobby).json() == {"status": "open"}

    def test_joined(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        match = start_shared_match(lobby, _member(club), _FakeRequest())
        assert _status(_client(host), lobby).json() == {
            "status": "joined",
            "match_url": reverse("pingpong:live_scoreboard", args=[match.pk]),
        }

    def test_expired(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        LiveLobby.all_objects.filter(pk=lobby.pk).update(expires_at=timezone.now())
        body = _status(_client(host), lobby).json()
        assert body["status"] == "expired" and body["reason"]

    def test_cancelled(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club, status=LiveLobby.Status.CANCELLED)
        assert _status(_client(host), lobby).json()["status"] == "cancelled"


@pytest.mark.django_db
class TestLobbyCancel:
    def test_cancel(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        resp = _client(host).post(reverse("pingpong:live_lobby_cancel", args=[lobby.pk]))
        lobby.refresh_from_db()
        assert lobby.status == LiveLobby.Status.CANCELLED
        assert resp.url == reverse("pingpong:play")

    def test_pick_opponent_instead_prefills_the_match_form(self, flags_on, club):
        host = _member(club)
        location = LocationFactory(club=club)
        lobby = LiveLobbyFactory(host=host, club=club, best_of=3, location=location)
        client = _client(host)
        resp = client.post(
            reverse("pingpong:live_lobby_cancel", args=[lobby.pk]), {"pick_opponent": "1"}
        )
        lobby.refresh_from_db()
        assert lobby.status == LiveLobby.Status.CANCELLED
        assert resp.url == f"{reverse('pingpong:match_add')}?best_of=3&location={location.pk}"
        form = client.get(resp.url).context["form"]
        assert form.initial["best_of"] == 3
        assert form.initial["location"] == location.pk

    def test_match_form_ignores_junk_prefill(self, flags_on, club):
        form = _client(_member(club)).get(
            reverse("pingpong:match_add") + "?best_of=4&location=x"
        ).context["form"]
        assert "location" not in form.initial
        assert form.initial.get("best_of") != 4

    def test_cancel_after_a_join_goes_to_the_scoreboard(self, flags_on, club):
        host = _member(club)
        lobby = LiveLobbyFactory(host=host, club=club)
        match = start_shared_match(lobby, _member(club), _FakeRequest())
        resp = _client(host).post(reverse("pingpong:live_lobby_cancel", args=[lobby.pk]))
        assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])

    def test_someone_elses_lobby_is_404(self, flags_on, club):
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        resp = _client(_member(club)).post(reverse("pingpong:live_lobby_cancel", args=[lobby.pk]))
        assert resp.status_code == 404
        lobby.refresh_from_db()
        assert lobby.is_open


# ---------------------------------------------------------------------------
# start_shared_match (KAN-152 §3)
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self):
        self.session = {}


@pytest.mark.django_db
class TestStartSharedMatch:
    def test_creates_the_match_and_both_scorer_rows(self, flags_on, club):
        host, joiner = _member(club), _member(club)
        location = LocationFactory(club=club)
        lobby = LiveLobbyFactory(host=host, club=club, best_of=3, location=location)
        match = start_shared_match(lobby, joiner, _FakeRequest())
        match = Match.all_objects.get(pk=match.pk)
        assert match.is_live and match.scorekeeper == host
        assert (match.best_of, match.location, match.club) == (3, location, club)
        assert list(match.side1_players) == [host]
        assert list(match.side2_players) == [joiner]
        rows = {r.player: r for r in LiveScorer.objects.filter(match=match)}
        assert (rows[host].side, rows[host].role, rows[host].can_score_both) == (
            Side.ONE, LiveScorer.Role.HOST, False
        )
        assert (rows[joiner].side, rows[joiner].role, rows[joiner].can_score_both) == (
            Side.TWO, LiveScorer.Role.GUEST, False
        )
        lobby.refresh_from_db()
        assert lobby.status == LiveLobby.Status.JOINED and lobby.match == match

    def test_two_joins_one_match(self, flags_on, club):
        """The conditional update, not threads: the second caller holds a
        stale copy that still says open."""
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        stale = LiveLobby.all_objects.get(pk=lobby.pk)
        first = start_shared_match(lobby, _member(club), _FakeRequest())
        second = start_shared_match(stale, _member(club), _FakeRequest())
        assert first is not None and second is None
        assert Match.all_objects.count() == 1
        assert LiveScorer.objects.count() == 2

    def test_an_expired_lobby_cannot_be_claimed(self, flags_on, club):
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        LiveLobby.all_objects.filter(pk=lobby.pk).update(expires_at=timezone.now())
        assert start_shared_match(lobby, _member(club), _FakeRequest()) is None
        assert not Match.all_objects.exists()

    def test_the_club_is_the_lobbys_not_the_joiners_active_one(self, flags_on, club):
        """The joiner is in two clubs and viewing the other one."""
        host = _member(club)
        joiner = _member(club)
        elsewhere = ClubFactory()
        ClubMembershipFactory(club=elsewhere, user=joiner.user)
        lobby = LiveLobbyFactory(host=host, club=club)
        client = _client(joiner)
        client.post(reverse("pingpong:club_switch"), {"club_id": elsewhere.pk})
        resp = client.post(_join_url(lobby))
        match = Match.all_objects.get()
        assert match.club == club
        assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])
        # ...and their active club followed, so the scoreboard is reachable.
        assert client.get(resp.url).status_code == 200


# ---------------------------------------------------------------------------
# The join view: every identity branch (KAN-152 §2)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestJoinView:
    def _lobby(self, club, **kwargs):
        return LiveLobbyFactory(host=_member(club, name="Ubaldo"), club=club, **kwargs)

    def test_unknown_token_is_a_real_404_page(self, flags_on):
        resp = Client().get(reverse("pingpong:live_join", args=["0" * 32]))
        assert resp.status_code == 404
        assert "This join link is not valid." in resp.content.decode()

    def test_an_overlong_token_is_not_looked_up(self):
        assert find_lobby("a" * 65) is None

    def test_anonymous_sees_who_and_gets_the_token_parked(self, flags_on, club):
        lobby = self._lobby(club, best_of=5)
        client = Client()
        resp = client.get(_join_url(lobby))
        html = resp.content.decode()
        assert resp.status_code == 200
        assert "Ubaldo wants to score a match with you" in html
        assert "Best of 5 at Thursday Club" in html
        assert client.session[PENDING_LIVE_JOIN_SESSION_KEY] == lobby.token
        # Log in comes back through the resume route; the token is in no URL.
        login_href = re.search(r'href="([^"]*accounts/login[^"]*)"', html).group(1)
        assert lobby.token not in login_href
        assert "next=" in login_href and "live/join/" in login_href.replace("%2F", "/")
        assert reverse("pingpong:signup") in html
        assert lobby.token not in html.replace(_join_url(lobby), "")

    def test_login_next_resumes_the_parked_join(self, flags_on, club):
        lobby = self._lobby(club)
        joiner = _member(club)
        client = Client()
        client.get(_join_url(lobby))
        resp = client.post(
            reverse("login") + "?next=" + reverse("pingpong:live_join_resume"),
            {"username": joiner.user.username, "password": "testpass123"},
        )
        assert resp.url == reverse("pingpong:live_join_resume")
        resp = client.get(resp.url)
        assert resp.url == _join_url(lobby)
        assert client.get(resp.url).context["state"] == "confirm"

    def test_resume_without_a_parked_token_goes_to_today(self, flags_on, club):
        resp = _client(_member(club)).get(reverse("pingpong:live_join_resume"))
        assert resp.url == reverse("pingpong:dashboard")

    def test_a_member_is_asked_to_confirm(self, flags_on, club):
        lobby = self._lobby(club)
        joiner = _member(club, name="Marco")
        resp = _client(joiner).get(_join_url(lobby))
        html = resp.content.decode()
        assert resp.context["state"] == "confirm"
        assert "Join as Marco?" in html
        assert joiner.hue_class in html
        assert "Not you? Log out" in html
        assert f'name="next" value="{_join_url(lobby)}"' in html
        assert not Match.all_objects.exists()  # GET never joins

    def test_not_you_log_out_comes_back_to_the_link(self, flags_on, club):
        lobby = self._lobby(club)
        client = _client(_member(club))
        resp = client.post(reverse("logout"), {"next": _join_url(lobby)})
        assert resp.url == _join_url(lobby)
        assert client.get(resp.url).context["state"] == "anonymous"

    def test_join_post_creates_the_match_and_goes_to_the_scoreboard(self, flags_on, club):
        lobby = self._lobby(club)
        joiner = _member(club)
        resp = _client(joiner).post(_join_url(lobby))
        match = Match.all_objects.get()
        assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])
        assert list(match.side2_players) == [joiner]

    def test_a_non_member_hits_a_clear_dead_end(self, flags_on, club):
        lobby = self._lobby(club)
        outsider = _member(ClubFactory())
        for resp in (_client(outsider).get(_join_url(lobby)), _client(outsider).post(_join_url(lobby))):
            assert resp.status_code == 200
            assert resp.context["state"] == "not_member"
            html = resp.content.decode()
            assert "You're not a member of Thursday Club" in html
            assert "Ask Ubaldo to send you a club invite" in html
        assert not Match.all_objects.exists()

    def test_the_host_is_told_it_is_their_own(self, flags_on, club):
        lobby = self._lobby(club)
        for resp in (_client(lobby.host).get(_join_url(lobby)), _client(lobby.host).post(_join_url(lobby))):
            assert resp.context["state"] == "own"
            assert "This is your own lobby" in resp.content.decode()
            assert _lobby_url(lobby) in resp.content.decode()
        assert not Match.all_objects.exists()

    @pytest.mark.parametrize("anonymous", [True, False])
    def test_expired(self, flags_on, club, anonymous):
        lobby = self._lobby(club)
        LiveLobby.all_objects.filter(pk=lobby.pk).update(expires_at=timezone.now())
        client = Client() if anonymous else _client(_member(club))
        resp = client.get(_join_url(lobby))
        assert resp.status_code == 410
        assert "This link has expired." in resp.content.decode()
        assert PENDING_LIVE_JOIN_SESSION_KEY not in client.session

    def test_cancelled(self, flags_on, club):
        lobby = self._lobby(club, status=LiveLobby.Status.CANCELLED)
        resp = _client(_member(club)).post(_join_url(lobby))
        assert resp.status_code == 410
        assert "This match was cancelled." in resp.content.decode()
        assert not Match.all_objects.exists()

    def test_joined_by_someone_else(self, flags_on, club):
        lobby = self._lobby(club)
        start_shared_match(lobby, _member(club), _FakeRequest())
        resp = _client(_member(club)).get(_join_url(lobby))
        assert resp.status_code == 410
        assert "Someone else has already joined this match." in resp.content.decode()

    def test_joined_by_this_player_rejoins_the_scoreboard(self, flags_on, club):
        lobby = self._lobby(club)
        joiner = _member(club)
        client = _client(joiner)
        client.post(_join_url(lobby))
        match = Match.all_objects.get()
        for resp in (client.get(_join_url(lobby)), client.post(_join_url(lobby))):
            assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])
        assert Match.all_objects.count() == 1

    def test_losing_the_race_shows_already_joined(self, flags_on, club, monkeypatch):
        """The claim fails after the view saw an open lobby."""
        lobby = self._lobby(club)
        from pingpong import live_lobby_views

        real = live_lobby_views.start_shared_match

        def racing(lobby_, joiner, request):
            real(LiveLobby.all_objects.get(pk=lobby_.pk), _member(club), _FakeRequest())
            return real(lobby_, joiner, request)

        monkeypatch.setattr(live_lobby_views, "start_shared_match", racing)
        resp = _client(_member(club)).post(_join_url(lobby))
        assert resp.status_code == 410
        assert Match.all_objects.count() == 1
        assert LiveScorer.objects.count() == 2

    def test_rate_limited_like_the_invite_accept(self, flags_on):
        client = Client()
        statuses = [
            client.get(reverse("pingpong:live_join", args=["0" * 32])).status_code
            for _ in range(22)
        ]
        assert statuses[0] == 404
        assert 403 in statuses

    def test_no_store(self, flags_on, club):
        lobby = self._lobby(club)
        assert "no-store" in Client().get(_join_url(lobby))["Cache-Control"]

    def test_the_token_is_never_logged(self, flags_on, club, caplog):
        lobby = self._lobby(club)
        joiner = _member(club)
        with caplog.at_level(logging.DEBUG):
            Client().get(_join_url(lobby))
            client = _client(joiner)
            client.get(_join_url(lobby))
            client.post(_join_url(lobby))
            _client(lobby.host).get(_lobby_url(lobby))
        assert lobby.token not in caplog.text


# ---------------------------------------------------------------------------
# The registration hook (hooked, not forked)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegistrationHook:
    def test_signup_with_an_invite_lands_on_the_confirm_page_logged_in(self, flags_on, club, mailoutbox):
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        invite = ClubInvite.issue(club=club, created_by=lobby.host.user)
        client = Client()
        client.get(reverse("pingpong:club_invite_accept", args=[invite.token]))
        client.get(_join_url(lobby))
        resp = client.post(reverse("pingpong:signup"), SIGNUP)
        assert resp.url == _join_url(lobby)
        user = User.objects.get(username="joiner")
        assert club.user_is_member(user)
        assert len(mailoutbox) == 1  # verification still sent
        assert not user.profile.email_verified  # and not required to join
        page = client.get(resp.url)
        assert page.context["state"] == "confirm"
        assert "Join as Marco Joiner?" in page.content.decode()

    def test_signup_without_membership_hits_the_dead_end(self, flags_on, club):
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        client = Client()
        client.get(_join_url(lobby))
        resp = client.post(reverse("pingpong:signup"), SIGNUP)
        assert resp.status_code == 200
        assert resp.context["state"] == "not_member"
        assert "joiner@example.com" in resp.content.decode()
        assert "_auth_user_id" not in client.session  # not logged in
        assert PENDING_LIVE_JOIN_SESSION_KEY not in client.session

    def test_a_dead_lobby_falls_back_to_normal_registration(self, flags_on, club):
        lobby = LiveLobbyFactory(host=_member(club), club=club)
        client = Client()
        client.get(_join_url(lobby))
        cancel_lobby(lobby)
        resp = client.post(reverse("pingpong:signup"), SIGNUP)
        assert resp.templates[0].name == "registration/verify_email_sent.html"
        assert "_auth_user_id" not in client.session

    def test_signup_without_a_parked_join_is_unchanged(self, flags_on):
        resp = Client().post(reverse("pingpong:signup"), SIGNUP)
        assert resp.templates[0].name == "registration/verify_email_sent.html"


# ---------------------------------------------------------------------------
# The required integration flow
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSharedScoringIntegration:
    """Lobby -> anonymous open -> register with a club invite in the session
    -> join -> both phones score their own side through live_sync -> the
    match completes -> both go to match_confirm -> confirmations as today."""

    def test_the_full_round_trip(self, flags_on, club, mailoutbox):
        host = _member(club, name="Ubaldo")
        host_client = _client(host)

        # 1. The host opens a lobby from the match form's button.
        resp = host_client.post(
            reverse("pingpong:live_lobby_create"), {"best_of": 1, "is_double": "False"}
        )
        lobby = LiveLobby.all_objects.get()
        assert resp.url == _lobby_url(lobby)
        assert host_client.get(resp.url).status_code == 200
        assert _status(host_client, lobby).json() == {"status": "open"}

        # 2. A stranger with a club invite opens the link, then signs up.
        invite = ClubInvite.issue(club=club, created_by=host.user)
        guest_client = Client()
        guest_client.get(reverse("pingpong:club_invite_accept", args=[invite.token]))
        assert guest_client.get(_join_url(lobby)).context["state"] == "anonymous"
        resp = guest_client.post(reverse("pingpong:signup"), SIGNUP)
        assert resp.url == _join_url(lobby)

        # 3. "Join as Marco Joiner?" -> Join match.
        assert guest_client.get(resp.url).context["state"] == "confirm"
        resp = guest_client.post(_join_url(lobby))
        match = Match.all_objects.get()
        scoreboard = reverse("pingpong:live_scoreboard", args=[match.pk])
        assert resp.url == scoreboard
        assert guest_client.get(scoreboard).status_code == 200

        # 4. The host's poll (and a reload) takes them to the same scoreboard.
        assert _status(host_client, lobby).json() == {"status": "joined", "match_url": scoreboard}
        assert host_client.get(_lobby_url(lobby)).url == scoreboard
        assert host_client.get(scoreboard).status_code == 200

        # 5. Each phone scores its own side on its own stream.
        assert _post(host_client, "live_start", match, {"initial_server": "team1", "seq": 1}).status_code == 200
        resp = _sync(guest_client, match, "team2", [1, 2, 3])
        assert resp.status_code == 200 and resp.json()["applied_seq"] == 3
        resp = _sync(host_client, match, "team1", range(2, 13))
        body = resp.json()
        confirm_url = reverse("pingpong:match_confirm", args=[match.pk])
        assert body["is_match_complete"] is True
        assert body["redirect_url"] == confirm_url

        # 6. The guest's phone learns it too: a resent op is answered with the
        #    completion, and the state poll says the match is over.
        resp = _sync(guest_client, match, "team2", [3])
        assert resp.json()["redirect_url"] == confirm_url
        assert guest_client.get(
            reverse("pingpong:live_state", args=[match.pk])
        ).json()["redirect_url"]

        # 7. Confirmations behave as today (the new account is unverified,
        #    so the match auto-confirmed when the winner was set).
        match = Match.all_objects.get(pk=match.pk)
        assert match.winner_side == Side.ONE
        assert (match.team1_score, match.team2_score) == (1, 0)
        for client in (host_client, guest_client):
            resp = client.get(confirm_url)
            assert resp.url == reverse("pingpong:match_detail", args=[match.pk])
        assert MatchConfirmation.objects.filter(match=match).count() == 2
        assert Match.all_objects.get(pk=match.pk).is_confirmed is True
        assert match.club == club

"""Tenancy observability (KAN-21 / T1.10).

The canary is a *check*, not an enforcement mechanism. The most important tests
here are the ones asserting it stays out of the way: that it logs rather than
raises, that it is off by default, and that a broken canary cannot break a page.
Turning a silent leak into a loud outage would be a worse trade than the leak.
"""
import logging

import pytest
from django.test import RequestFactory

from pingpong import observability
from pingpong.models import Match, Player
from ttstats.middleware import (
    REQUEST_LOCAL_ATTRS,
    CurrentUserMiddleware,
    _thread_locals,
)

from .conftest import ClubFactory, ClubMembershipFactory, MatchFactory, PlayerFactory, UserFactory


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
def canary_armed(settings):
    settings.FEATURE_FLAGS = {"clubs": True, "tenancy_canary": True}
    settings.TENANCY_CANARY_SAMPLE_RATE = 1.0


class TestCanaryIsOffByDefault:
    """It runs inside `get_queryset()`, the hottest path in the app."""

    def test_disabled_when_the_flag_is_off(self, settings):
        settings.FEATURE_FLAGS = {"tenancy_canary": False}
        settings.TENANCY_CANARY_SAMPLE_RATE = 1.0
        assert observability.canary_enabled() is False

    def test_disabled_when_the_sample_rate_is_zero(self, settings):
        settings.FEATURE_FLAGS = {"tenancy_canary": True}
        settings.TENANCY_CANARY_SAMPLE_RATE = 0.0
        assert observability.canary_enabled() is False

    def test_both_gates_must_pass(self, settings):
        settings.FEATURE_FLAGS = {"tenancy_canary": True}
        settings.TENANCY_CANARY_SAMPLE_RATE = 1.0
        assert observability.canary_enabled() is True

    def test_an_undeclared_flag_leaves_it_off(self, settings):
        """Fail closed, like every other flag."""
        settings.FEATURE_FLAGS = {}
        settings.TENANCY_CANARY_SAMPLE_RATE = 1.0
        assert observability.canary_enabled() is False


@pytest.mark.django_db
class TestCanaryFiring:
    def test_it_fires_on_a_deliberately_unscoped_queryset(self, caplog):
        club, other = ClubFactory(), ClubFactory()
        PlayerFactory(club=club)
        PlayerFactory(club=other)

        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            observability.check_queryset_scope(
                Player.objects.all(), club, "club", "deliberate-leak"
            )

        assert any("TENANCY CANARY" in r.getMessage() for r in caplog.records)

    def test_it_does_not_fire_on_a_correctly_scoped_queryset(self, caplog):
        club, other = ClubFactory(), ClubFactory()
        PlayerFactory(club=club)
        PlayerFactory(club=other)

        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            observability.check_queryset_scope(
                Player.objects.filter(club=club), club, "club", "scoped"
            )

        assert not [r for r in caplog.records if "TENANCY CANARY" in r.getMessage()]

    def test_the_message_names_the_source_and_the_club(self, caplog):
        """A canary you cannot act on is noise, and noise gets ignored — which
        is the same as not having one."""
        club, other = ClubFactory(), ClubFactory()
        PlayerFactory(club=other)

        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            observability.check_queryset_scope(
                Player.objects.all(), club, "club", "PlayerManager.get_queryset"
            )

        message = caplog.records[-1].getMessage()
        assert "PlayerManager.get_queryset" in message
        assert str(club.pk) in message

    def test_it_does_nothing_when_there_is_no_club(self, caplog):
        """Management commands and cron jobs have no request, user or club."""
        PlayerFactory()
        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            observability.check_queryset_scope(
                Player.objects.all(), None, "club", "no-club"
            )
        assert not caplog.records

    def test_it_is_bounded(self, caplog):
        """Noticing a leak does not require enumerating it, and an unbounded
        scan would cost more than the page it is watching."""
        club, other = ClubFactory(), ClubFactory()
        for _ in range(observability.CANARY_SAMPLE_ROWS + 10):
            PlayerFactory(club=other)

        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            observability.check_queryset_scope(
                Player.objects.all(), club, "club", "bounded"
            )

        message = caplog.records[-1].getMessage()
        assert f"{observability.CANARY_SAMPLE_ROWS} row(s)" in message


@pytest.mark.django_db
class TestCanaryNeverBreaksAnything:
    """Turning a silent leak into a loud outage is the worse trade."""

    def test_it_logs_rather_than_raises_on_a_leak(self, caplog):
        club, other = ClubFactory(), ClubFactory()
        PlayerFactory(club=other)

        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            observability.check_queryset_scope(
                Player.objects.all(), club, "club", "leak"
            )  # must not raise

    def test_an_unusable_queryset_is_swallowed(self, caplog):
        """A sliced queryset cannot be re-filtered. A canary that raises on one
        would take down whichever page happened to paginate."""
        club = ClubFactory()
        PlayerFactory(club=club)

        observability.check_queryset_scope(
            Player.objects.all()[:1], club, "club", "sliced"
        )

    def test_a_nonsense_lookup_is_swallowed(self):
        club = ClubFactory()
        observability.check_queryset_scope(
            Player.objects.all(), club, "no_such_field", "bad-lookup"
        )

    def test_pages_still_render_with_the_canary_armed(self, canary_armed, client):
        user = UserFactory()
        club = ClubFactory()
        PlayerFactory(user=user, club=club)
        ClubMembershipFactory(club=club, user=user)
        client.force_login(user)

        from django.urls import reverse

        assert client.get(reverse("pingpong:player_list")).status_code == 200
        assert client.get(reverse("pingpong:leaderboard")).status_code == 200


@pytest.mark.django_db
class TestRequestContextFilter:
    def _record(self):
        return logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )

    def test_attributes_are_always_set_outside_a_request(self):
        """A formatter naming them must not blow up on a record from a
        management command, a cron job, or an exception during startup."""
        record = self._record()
        observability.RequestContextFilter().filter(record)

        assert record.request_path == "-"
        assert record.user_id == "-"
        assert record.club_id == "-"

    def test_it_carries_the_path_user_and_club_during_a_request(self):
        user = UserFactory()
        club = ClubFactory()
        ClubMembershipFactory(club=club, user=user)

        captured = {}

        def inner(request):
            from ttstats.middleware import get_current_club

            get_current_club()
            record = self._record()
            observability.RequestContextFilter().filter(record)
            captured.update(
                path=record.request_path, user=record.user_id, club=record.club_id
            )
            return "response"

        request = RequestFactory().get("/pingpong/leaderboard/")
        request.user = user
        request.session = {}
        CurrentUserMiddleware(inner)(request)

        assert captured["path"] == "/pingpong/leaderboard/"
        assert captured["user"] == str(user.pk)
        assert captured["club"] == str(club.pk)

    def test_it_logs_ids_never_identities(self):
        """A log aggregator is a copy of your data in somebody else's system."""
        user = UserFactory(username="alice", email="alice@example.com")
        club = ClubFactory(name="Secret Club")

        def inner(request):
            from ttstats.middleware import get_current_club

            get_current_club()
            record = self._record()
            observability.RequestContextFilter().filter(record)
            formatted = logging.Formatter(
                "path=%(request_path)s user=%(user_id)s club=%(club_id)s %(message)s"
            ).format(record)

            assert "alice" not in formatted
            assert "alice@example.com" not in formatted
            assert "Secret Club" not in formatted
            return "response"

        ClubMembershipFactory(club=club, user=user)
        request = RequestFactory().get("/pingpong/")
        request.user = user
        request.session = {}
        CurrentUserMiddleware(inner)(request)

    def test_the_query_string_is_not_logged(self):
        """It can carry tokens — the email-verification flow puts one in a URL."""
        user = UserFactory()

        def inner(request):
            record = self._record()
            observability.RequestContextFilter().filter(record)
            assert "token" not in record.request_path
            assert "secret" not in record.request_path
            return "response"

        request = RequestFactory().get("/pingpong/?token=secret-value")
        request.user = user
        request.session = {}
        CurrentUserMiddleware(inner)(request)

    def test_the_request_path_does_not_leak_to_the_next_request(self):
        user = UserFactory()

        def inner(request):
            return "response"

        request = RequestFactory().get("/pingpong/leaderboard/")
        request.user = user
        request.session = {}
        CurrentUserMiddleware(inner)(request)

        assert not hasattr(_thread_locals, "request_path")

    def test_the_filter_never_raises(self, monkeypatch):
        """Logging must never be the thing that fails."""
        import ttstats.middleware as mw

        class Exploding:
            def __getattr__(self, item):
                raise RuntimeError("boom")

        monkeypatch.setattr(mw, "_thread_locals", Exploding())
        record = self._record()

        assert observability.RequestContextFilter().filter(record) is True
        assert record.club_id == "-"


@pytest.mark.django_db
class TestCanaryInTheManagerMixin:
    """It lives in the shared mixin so there is one place to arm it."""

    def test_a_correctly_scoped_manager_query_stays_quiet(self, canary_armed, caplog):
        user = UserFactory()
        club = ClubFactory()
        player = PlayerFactory(user=user, club=club)
        ClubMembershipFactory(club=club, user=user)
        PlayerFactory(club=ClubFactory())

        _thread_locals.user = user
        _thread_locals.session = {}

        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            list(Player.objects.all())
            list(Match.objects.all())

        assert not [r for r in caplog.records if "TENANCY CANARY" in r.getMessage()]

    def test_it_stays_quiet_when_there_is_no_viewer(self, canary_armed, caplog):
        """Management commands are unfiltered on purpose. A canary that fires
        on every intentional case gets ignored, which is the same as not
        having one."""
        PlayerFactory(club=ClubFactory())

        with caplog.at_level(logging.ERROR, logger="pingpong.tenancy"):
            list(Player.objects.all())

        assert not caplog.records

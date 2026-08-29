"""Sentry wiring (ttstats/sentry.py).

The important tests here are the negative ones. Sentry is a third party in the
request path, so what has to hold is that it stays out of the way when it is
not configured, that it cannot raise, and above all that it never carries a
bearer token or an identity off the box. Whether an event renders nicely in the
Sentry UI is not something a test can tell us; whether it contains an invite
token is.
"""
import logging

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from ttstats import sentry
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import ClubFactory, PlayerFactory, UserFactory


@pytest.fixture(autouse=True)
def _clean_context():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


class TestInitIsOffWithoutADSN:
    def test_no_dsn_means_no_sdk(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        assert sentry.init_sentry() is False

    def test_blank_dsn_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "   ")
        assert sentry.init_sentry() is False


class TestScrubUrl:
    """The invite token and the email-verification token are credentials.

    Both sit in the URL *path*, so dropping the query string is not enough.
    """

    def test_redacts_a_club_invite_token(self):
        scrubbed = sentry.scrub_url("https://x.test/pingpong/join/deadbeefcafe/")
        assert "deadbeefcafe" not in scrubbed
        assert scrubbed == "https://x.test/pingpong/join/[redacted]/"

    def test_redacts_an_email_verification_token(self):
        scrubbed = sentry.scrub_url("/pingpong/verify-email/abc123/")
        assert "abc123" not in scrubbed

    def test_drops_the_query_string_wholesale(self):
        assert sentry.scrub_url("/pingpong/matches/?next=/x&token=s3cret") == (
            "/pingpong/matches/"
        )

    def test_leaves_an_ordinary_path_alone(self):
        assert sentry.scrub_url("/pingpong/matches/12/") == "/pingpong/matches/12/"

    def test_a_token_in_the_query_of_a_token_path_goes_too(self):
        scrubbed = sentry.scrub_url("/pingpong/join/tok/?ref=mail")
        assert "tok" not in scrubbed and "ref" not in scrubbed

    def test_non_strings_pass_through(self):
        assert sentry.scrub_url(None) is None


class TestBeforeSend:
    def test_scrubs_the_request_url_and_query_string(self):
        event = sentry.before_send(
            {"request": {"url": "/pingpong/join/tok/", "query_string": "token=tok"}}, {}
        )
        assert event["request"]["url"] == "/pingpong/join/[redacted]/"
        assert event["request"]["query_string"] == ""

    def test_drops_cookie_and_referer_headers(self):
        event = sentry.before_send(
            {"request": {"headers": {"Cookie": "sessionid=x", "Referer": "/join/tok/"}}},
            {},
        )
        assert event["request"]["headers"] == {}

    def test_scrubs_breadcrumb_urls(self):
        event = sentry.before_send(
            {"breadcrumbs": {"values": [{"data": {"url": "/pingpong/join/tok/"}}]}}, {}
        )
        assert event["breadcrumbs"]["values"][0]["data"]["url"].endswith("[redacted]/")

    @pytest.mark.django_db
    def test_tags_the_viewer_and_club_ids_only(self):
        user = UserFactory()
        club = ClubFactory()
        _thread_locals.user = user
        _thread_locals.club = club

        event = sentry.before_send({}, {})

        assert event["tags"] == {"user_id": str(user.pk), "club_id": str(club.pk)}
        # Ids, never identities -- the same rule prod.py's log formatter follows.
        assert user.username not in str(event)
        assert user.email not in str(event)

    @pytest.mark.django_db
    def test_does_not_resolve_a_club_that_was_never_resolved(self, monkeypatch):
        """An unresolved club is worth no tag at all.

        Calling get_current_club() here would issue a membership query from
        inside an exception handler, on the connection that may have just
        failed. The event goes out untagged instead.
        """
        _thread_locals.user = UserFactory()

        def _explode():  # pragma: no cover - must never be called
            raise AssertionError("before_send resolved the club lazily")

        monkeypatch.setattr("ttstats.middleware.get_current_club", _explode)

        assert "club_id" not in sentry.before_send({}, {}).get("tags", {})

    def test_survives_a_malformed_event(self):
        """Telemetry never raises. A weird event is sent as-is, not dropped."""
        event = {"request": "not-a-dict", "breadcrumbs": {"values": None}}
        assert sentry.before_send(event, {}) is event


class TestCapture4xxStatuses:
    def test_empty_by_default(self, monkeypatch):
        monkeypatch.delenv("SENTRY_CAPTURE_4XX", raising=False)
        assert sentry.capture_4xx_statuses() == set()

    def test_parses_a_list(self, monkeypatch):
        monkeypatch.setenv("SENTRY_CAPTURE_4XX", "403, 405")
        assert sentry.capture_4xx_statuses() == {403, 405}

    def test_junk_entries_are_dropped_not_raised(self, monkeypatch):
        monkeypatch.setenv("SENTRY_CAPTURE_4XX", "403,nonsense,")
        assert sentry.capture_4xx_statuses() == {403}


class TestSentryHttpErrorMiddleware:
    def _middleware(self, status, monkeypatch, captured, configure="403"):
        monkeypatch.setenv("SENTRY_CAPTURE_4XX", configure)
        def _capture(self, request, response):
            captured.append(response.status_code)

        monkeypatch.setattr(sentry.SentryHttpErrorMiddleware, "_capture", _capture)
        return sentry.SentryHttpErrorMiddleware(lambda r: HttpResponse(status=status))

    def test_reports_a_configured_status(self, monkeypatch):
        captured = []
        mw = self._middleware(403, monkeypatch, captured)
        assert mw(RequestFactory().get("/x/")).status_code == 403
        assert captured == [403]

    def test_ignores_a_status_that_was_not_configured(self, monkeypatch):
        captured = []
        mw = self._middleware(404, monkeypatch, captured)
        mw(RequestFactory().get("/x/"))
        assert captured == []

    def test_reports_nothing_when_unconfigured(self, monkeypatch):
        captured = []
        mw = self._middleware(403, monkeypatch, captured, configure="")
        mw(RequestFactory().get("/x/"))
        assert captured == []

    def test_a_broken_sdk_cannot_break_the_response(self, monkeypatch):
        """The real _capture, with sentry_sdk absent, must still return the page."""
        monkeypatch.setenv("SENTRY_CAPTURE_4XX", "403")
        monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", None)
        mw = sentry.SentryHttpErrorMiddleware(lambda r: HttpResponse(status=403))
        assert mw(RequestFactory().get("/x/")).status_code == 403


class TestEnvFloat:
    def test_malformed_rate_degrades_to_the_default(self, monkeypatch, caplog):
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "half")
        with caplog.at_level(logging.WARNING):
            assert sentry._env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0) == 0.0

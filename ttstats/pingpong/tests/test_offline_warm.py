"""Background warm-up of the core pages (KAN-136, S12.5).

The server's half: which URLs a page hands the worker (the `snapshot-warm`
json_script), that every one is an allowlisted read-only page, what the list
costs in queries, and that a warm-up fetch (`X-TTStats-Warm: 1`) has no side
effects. The worker's half is tests/js/sw_warm.test.mjs; the browser's,
tests/e2e/offline-warm.pwa.spec.js.
"""
import json
import re
from datetime import timedelta

import pytest
from django.contrib import messages
from django.db import connection
from django.contrib.messages.storage.base import Message
from django.contrib.messages.storage.cookie import CookieStorage
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

from pingpong import offline
from pingpong.models import FeatureFlagOverride, Match
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import MatchFactory, PlayerFactory, UserFactory

WARM = {"HTTP_X_TTSTATS_WARM": "1"}


@pytest.fixture(autouse=True)
def _no_thread_local_leak():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


def _flag(user, enabled=True):
    # No factory exists for overrides; this is the rollout mechanism itself.
    FeatureFlagOverride.objects.create(name=offline.SNAPSHOT_FLAG, user=user, enabled=enabled)


def _client(player, flag=True):
    if flag is not None:
        _flag(player.user, flag)
    client = Client()
    client.force_login(player.user)
    return client


def _warm_list(html):
    found = re.search(r'<script id="snapshot-warm" type="application/json">(.*?)</script>', html, re.S)
    return None if found is None else json.loads(found.group(1))


def _matches(viewer, n, start=None):
    """`n` confirmed matches for `viewer`, oldest first."""
    start = start or timezone.now() - timedelta(days=30)
    return [
        MatchFactory(player1=viewer, player2=PlayerFactory(), confirmed=True, date_played=start + timedelta(days=i))
        for i in range(n)
    ]


@pytest.mark.django_db
class TestWarmList:
    def test_today_table_own_page_and_last_three_confirmed_matches(self):
        viewer = PlayerFactory(with_user=True)
        played = _matches(viewer, 5)
        html = _client(viewer).get(reverse("pingpong:dashboard")).content.decode()

        assert _warm_list(html) == [
            reverse("pingpong:dashboard"),
            reverse("pingpong:leaderboard"),
            reverse("pingpong:player_detail", args=[viewer.pk]),
            *[reverse("pingpong:match_detail", args=[m.pk]) for m in reversed(played[-3:])],
        ]

    def test_unconfirmed_and_other_peoples_matches_are_left_out(self):
        viewer = PlayerFactory(with_user=True)
        opponent = PlayerFactory(with_user=True)
        for player in (viewer, opponent):
            # Verified players: their match waits for confirmation.
            player.user.profile.email_verified = True
            player.user.profile.save()
        MatchFactory(player1=PlayerFactory(), player2=PlayerFactory(), confirmed=True)
        pending = MatchFactory(player1=viewer, player2=opponent)
        assert not Match.all_objects.get(pk=pending.pk).is_confirmed
        (mine,) = _matches(viewer, 1)
        urls = _warm_list(_client(viewer).get(reverse("pingpong:dashboard")).content.decode())

        assert reverse("pingpong:match_detail", args=[mine.pk]) in urls
        assert reverse("pingpong:match_detail", args=[pending.pk]) not in urls
        assert len(urls) == 4

    def test_at_most_six(self):
        viewer = PlayerFactory(with_user=True)
        _matches(viewer, 8)
        urls = _warm_list(_client(viewer).get(reverse("pingpong:dashboard")).content.decode())
        assert len(urls) == offline.WARM_MAX == 6

    def test_a_user_without_a_player_gets_today_and_the_table(self):
        user = UserFactory()
        _flag(user)
        _thread_locals.user = user
        assert offline.warm_urls(user) == [reverse("pingpong:dashboard"), reverse("pingpong:leaderboard")]

    def test_anonymous_gets_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        assert offline.warm_urls(AnonymousUser()) == []
        assert offline.warm_urls(None) == []

    def test_every_warm_url_is_an_allowlisted_snapshot_page(self):
        viewer = PlayerFactory(with_user=True)
        _matches(viewer, 3)
        urls = _warm_list(_client(viewer).get(reverse("pingpong:dashboard")).content.decode())
        assert len(urls) == 6
        for url in urls:
            match = resolve(url)
            assert f"{match.namespace}:{match.url_name}" in offline.SNAPSHOT_ALLOWLIST, url
            assert offline.is_snapshot_view(match.func), url

    def test_on_every_base_page_with_the_script(self):
        viewer = PlayerFactory(with_user=True)
        html = _client(viewer).get(reverse("pingpong:leaderboard")).content.decode()
        assert _warm_list(html) is not None
        assert 'src="/static/pingpong/js/snapshot_warm.js"' in html

    def test_flag_off_no_list_and_no_script(self):
        viewer = PlayerFactory(with_user=True)
        for flag in (False, None):
            html = _client(viewer, flag=flag).get(reverse("pingpong:dashboard")).content.decode()
            assert _warm_list(html) is None
            assert "snapshot_warm.js" not in html

    def test_a_warm_fetch_carries_no_list_of_its_own(self):
        viewer = PlayerFactory(with_user=True)
        html = _client(viewer).get(reverse("pingpong:dashboard"), **WARM).content.decode()
        assert _warm_list(html) is None


@pytest.mark.django_db
class TestWarmListCost:
    def _queries(self, client, url):
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(url).status_code == 200
        return len(ctx.captured_queries)

    @pytest.mark.parametrize("name", ["pingpong:dashboard", "pingpong:leaderboard"])
    def test_one_query_added_to_a_page(self, name, monkeypatch):
        viewer = PlayerFactory(with_user=True)
        _matches(viewer, 4)
        client = _client(viewer)
        url = reverse(name)
        client.get(url)  # warm Django's own caches (flags, leaderboard)

        with_list = self._queries(client, url)
        monkeypatch.setattr(offline, "warm_urls", lambda user: [])
        without = self._queries(client, url)
        assert with_list - without == 1

    def test_the_list_itself_is_one_query(self, django_assert_num_queries):
        viewer = PlayerFactory(with_user=True)
        _matches(viewer, 4)
        user = type(viewer.user).objects.get(pk=viewer.user.pk)
        user.player  # loaded by base.html before the list anyway
        _thread_locals.user = user
        offline.warm_urls(user)  # memoizes the request's club, as a page already has
        with django_assert_num_queries(1):
            assert len(offline.warm_urls(user)) == 6


WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.I)


@pytest.mark.django_db
class TestWarmFetchHasNoSideEffects:
    """Audit: every warm URL is a GET with no write behind it, and a warm
    fetch must not use up a flash message the user hasn't seen."""

    def test_no_database_writes_on_any_warm_url(self):
        viewer = PlayerFactory(with_user=True)
        _matches(viewer, 3)
        client = _client(viewer)
        urls = _warm_list(client.get(reverse("pingpong:dashboard")).content.decode())
        for url in urls:
            client.get(url, **WARM)  # first fill of any read cache
            with CaptureQueriesContext(connection) as ctx:
                response = client.get(url, **WARM)
            assert response.status_code == 200, url
            assert response[offline.SNAPSHOT_HEADER] == "1", url
            writes = [
                q["sql"] for q in ctx.captured_queries
                if WRITE.match(q["sql"]) and "django_session" not in q["sql"]
            ]
            assert writes == [], (url, writes)

    def test_a_pending_flash_message_survives_a_warm_fetch(self):
        viewer = PlayerFactory(with_user=True)
        client = _client(viewer)
        # A message waiting for the next page the user opens (as a redirect
        # after a POST leaves it, in the messages cookie).
        storage = CookieStorage(RequestFactory().get("/"))
        client.cookies[storage.cookie_name] = storage._encode(
            [Message(messages.SUCCESS, "Match saved for you")]
        )

        warm = client.get(reverse("pingpong:dashboard"), **WARM).content.decode()
        assert "Match saved for you" not in warm
        visit = client.get(reverse("pingpong:dashboard")).content.decode()
        assert "Match saved for you" in visit

    def test_a_warm_fetch_is_still_a_snapshot_for_this_identity(self):
        viewer = PlayerFactory(with_user=True)
        client = _client(viewer)
        normal = client.get(reverse("pingpong:dashboard"))
        warm = client.get(reverse("pingpong:dashboard"), **WARM)
        assert warm[offline.SNAPSHOT_HEADER] == "1"
        assert warm[offline.IDENTITY_HEADER] == normal[offline.IDENTITY_HEADER]

    def test_header_constant(self):
        assert offline.WARM_HEADER == "X-TTStats-Warm"


def test_match_ordering_assumption():
    # warm_urls orders explicitly; this pins that "last" means date_played.
    assert Match._meta.ordering == ["-date_played"]

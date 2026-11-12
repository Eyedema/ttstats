"""Server opt-in for offline snapshots (KAN-132, S12.1).

The service worker stores a read-only page for offline reading only when the
server says it may: `X-TTStats-Snapshot: 1`, sent by `@offline_snapshot` on an
allowlist of views, with the flag on. pytest cannot run the worker (that is
KAN-133's e2e); these pin what Python owns: the decorator's branches, the
allowlist (and that nothing outside it opts in), the identity it files pages
under, and the marker string the worker rewrites.
"""
import re

import pytest
from django.http import HttpResponse, JsonResponse
from django.test import Client, RequestFactory
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from django.views.generic import View

from pingpong import offline
from pingpong.models import FeatureFlagOverride
from ttstats.middleware import ACTIVE_CLUB_SESSION_KEY, REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import (
    ChampionshipFactory,
    ClubFactory,
    ClubMembershipFactory,
    MatchFactory,
    PlayerFactory,
    SeasonFactory,
    UserFactory,
)

HEX16 = re.compile(r"^[0-9a-f]{16}$")
FLAG = offline.SNAPSHOT_FLAG


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
    FeatureFlagOverride.objects.create(name=FLAG, user=user, enabled=enabled)


def _html(request):
    return HttpResponse("<p>page</p>", content_type="text/html; charset=utf-8")


def _request(user, method="get", **headers):
    request = getattr(RequestFactory(), method)("/pingpong/", headers=headers)
    request.user = user
    return request


# ---------------------------------------------------------------------------
# Constants the worker and the templates rely on
# ---------------------------------------------------------------------------

class TestConstants:
    def test_header_names(self):
        assert offline.SNAPSHOT_HEADER == "X-TTStats-Snapshot"
        assert offline.IDENTITY_HEADER == "X-TTStats-Identity"

    def test_freshness_marker_is_the_exact_attribute(self):
        # sw.js string-replaces this, byte for byte (KAN-134).
        assert offline.FRESHNESS_MARKER == 'data-freshness="network"'

    def test_flag_is_declared_and_off_by_default(self):
        from django.conf import settings

        assert settings.FEATURE_FLAGS[FLAG] is False

    def test_allowlist_is_a_frozenset_of_namespaced_names(self):
        assert isinstance(offline.SNAPSHOT_ALLOWLIST, frozenset)
        assert all(name.startswith("pingpong:") for name in offline.SNAPSHOT_ALLOWLIST)


# ---------------------------------------------------------------------------
# The decorator, branch by branch
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDecoratorBranches:
    def _user(self, enabled=True):
        user = UserFactory()
        _flag(user, enabled)
        return user

    def test_flag_on_full_page_get_is_marked(self):
        user = self._user()
        response = offline.offline_snapshot(_html)(_request(user))

        assert response[offline.SNAPSHOT_HEADER] == "1"
        assert HEX16.match(response[offline.IDENTITY_HEADER])
        assert "private" in response["Cache-Control"]
        assert "public" not in response["Cache-Control"]

    def test_identity_header_matches_request_identity(self):
        user = self._user()
        request = _request(user)
        response = offline.offline_snapshot(_html)(request)
        assert response[offline.IDENTITY_HEADER] == offline.request_identity(request)

    def test_a_public_cache_control_is_made_private(self):
        user = self._user()

        def public(request):
            response = _html(request)
            response["Cache-Control"] = "public, max-age=60"
            return response

        cc = offline.offline_snapshot(public)(_request(user))["Cache-Control"]
        assert "private" in cc
        assert "public" not in cc

    def test_post_is_not_marked(self):
        user = self._user()
        response = offline.offline_snapshot(_html)(_request(user, method="post"))
        assert offline.SNAPSHOT_HEADER not in response
        assert offline.IDENTITY_HEADER not in response

    def test_head_is_not_marked(self):
        user = self._user()
        response = offline.offline_snapshot(_html)(_request(user, method="head"))
        assert offline.SNAPSHOT_HEADER not in response

    @pytest.mark.parametrize("status", [201, 204, 301, 302, 304, 403, 404, 500, 502])
    def test_non_200_is_not_marked(self, status):
        user = self._user()

        def view(request):
            return HttpResponse("x", status=status, content_type="text/html")

        response = offline.offline_snapshot(view)(_request(user))
        assert offline.SNAPSHOT_HEADER not in response
        assert offline.IDENTITY_HEADER not in response

    def test_non_html_is_not_marked(self):
        user = self._user()
        response = offline.offline_snapshot(lambda r: JsonResponse({}))(_request(user))
        assert offline.SNAPSHOT_HEADER not in response

    def test_htmx_request_is_not_marked(self):
        user = self._user()
        response = offline.offline_snapshot(_html)(_request(user, **{"HX-Request": "true"}))
        assert offline.SNAPSHOT_HEADER not in response
        assert offline.IDENTITY_HEADER not in response
        assert not response.has_header("Cache-Control")

    def test_flag_off_is_not_marked(self):
        user = self._user(enabled=False)
        response = offline.offline_snapshot(_html)(_request(user))
        assert offline.SNAPSHOT_HEADER not in response
        assert not response.has_header("Cache-Control")

    def test_flag_default_off_is_not_marked(self):
        response = offline.offline_snapshot(_html)(_request(UserFactory()))
        assert offline.SNAPSHOT_HEADER not in response

    def test_anonymous_is_never_marked_even_with_the_flag_on_globally(self, settings):
        from django.contrib.auth.models import AnonymousUser

        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, FLAG: True}
        response = offline.offline_snapshot(_html)(_request(AnonymousUser()))
        assert offline.SNAPSHOT_HEADER not in response
        assert offline.IDENTITY_HEADER not in response

    def test_a_decorated_function_carries_the_marker(self):
        assert offline.is_snapshot_view(offline.offline_snapshot(_html))
        assert not offline.is_snapshot_view(_html)

    def test_a_decorated_class_works_through_as_view(self):
        class Page(View):
            def get(self, request):
                return _html(request)

            def post(self, request):
                return _html(request)

        decorated = offline.offline_snapshot(Page)
        assert decorated is Page
        callback = Page.as_view()
        assert offline.is_snapshot_view(callback)

        user = self._user()
        assert callback(_request(user))[offline.SNAPSHOT_HEADER] == "1"
        assert offline.SNAPSHOT_HEADER not in callback(_request(user, method="post"))


# ---------------------------------------------------------------------------
# The identity it files pages under
# ---------------------------------------------------------------------------

def _dashboard_identity(client):
    response = client.get(reverse("pingpong:dashboard"))
    assert response.status_code == 200
    return response[offline.IDENTITY_HEADER]


@pytest.mark.django_db
class TestFingerprint:
    def _client(self, player):
        _flag(player.user)
        client = Client()
        client.force_login(player.user)
        return client

    def test_stable_across_requests(self):
        client = self._client(PlayerFactory(with_user=True))
        first = _dashboard_identity(client)
        assert HEX16.match(first)
        assert _dashboard_identity(client) == first

    def test_differs_across_users(self):
        a = _dashboard_identity(self._client(PlayerFactory(with_user=True)))
        b = _dashboard_identity(self._client(PlayerFactory(with_user=True)))
        assert a != b

    def test_differs_across_the_active_club_of_one_user(self):
        player = PlayerFactory(with_user=True)
        alpha, zulu = ClubFactory(name="Alpha"), ClubFactory(name="Zulu")
        ClubMembershipFactory(club=alpha, user=player.user)
        ClubMembershipFactory(club=zulu, user=player.user)
        client = self._client(player)

        identities = []
        for club in (alpha, zulu, alpha):
            session = client.session
            session[ACTIVE_CLUB_SESSION_KEY] = club.pk
            session.save()
            identities.append(_dashboard_identity(client))

        assert identities[0] != identities[1]
        assert identities[0] == identities[2]

    def test_header_agrees_with_the_page_meta(self):
        # The worker files the copy under the header; the page later
        # announces the meta. They must agree or the copy deletes itself.
        client = self._client(PlayerFactory(with_user=True))
        response = client.get(reverse("pingpong:dashboard"))
        meta = re.search(r'<meta name="ttstats-identity" content="([^"]*)">', response.content.decode())
        assert meta and meta.group(1) == response[offline.IDENTITY_HEADER]


# ---------------------------------------------------------------------------
# The allowlist, through the whole stack
# ---------------------------------------------------------------------------

def _allowlisted_urls():
    """One reachable URL per allowlisted name, for a viewer who can see it."""
    viewer = PlayerFactory(with_user=True)
    viewer.user.profile.email_verified = True
    viewer.user.profile.save()
    opponent = PlayerFactory(with_user=True)
    match = MatchFactory(player1=viewer, player2=opponent, confirmed=True)
    championship = ChampionshipFactory(with_entries=[[viewer], [opponent]], created_by=viewer)
    season = SeasonFactory(archived_at=timezone.now())

    urls = {
        "pingpong:dashboard": reverse("pingpong:dashboard"),
        "pingpong:leaderboard": reverse("pingpong:leaderboard"),
        "pingpong:match_list": reverse("pingpong:match_list"),
        "pingpong:match_detail": reverse("pingpong:match_detail", args=[match.pk]),
        "pingpong:player_detail": reverse("pingpong:player_detail", args=[opponent.pk]),
        "pingpong:head_to_head": reverse("pingpong:head_to_head")
        + f"?player1={viewer.pk}&player2={opponent.pk}",
        "pingpong:championship_list": reverse("pingpong:championship_list"),
        "pingpong:championship_detail": reverse("pingpong:championship_detail", args=[championship.pk]),
        "pingpong:calendar": reverse("pingpong:calendar"),
        "pingpong:season_archive": reverse("pingpong:season_archive"),
        "pingpong:season_archive_detail": reverse("pingpong:season_archive_detail", args=[season.pk]),
    }
    assert set(urls) == offline.SNAPSHOT_ALLOWLIST, "add a URL here for every allowlisted view"
    return viewer, urls


ALLOWLIST = sorted(offline.SNAPSHOT_ALLOWLIST)


@pytest.mark.django_db
class TestAllowlistedViews:
    def _client(self, player):
        client = Client()
        client.force_login(player.user)
        return client

    @pytest.mark.parametrize("name", ALLOWLIST)
    def test_flag_on_full_page_get_sends_the_header(self, name):
        viewer, urls = _allowlisted_urls()
        _flag(viewer.user)

        response = self._client(viewer).get(urls[name])

        assert response.status_code == 200
        assert response[offline.SNAPSHOT_HEADER] == "1"
        assert HEX16.match(response[offline.IDENTITY_HEADER])
        assert "private" in response["Cache-Control"]

    @pytest.mark.parametrize("name", ALLOWLIST)
    def test_flag_on_cache_warm_second_load_still_sends_it(self, name):
        # Several of these pages read server-side caches (leaderboard,
        # player stats, h2h, dashboard recent matches). A cache hit is the
        # same page and must carry the same opt-in.
        viewer, urls = _allowlisted_urls()
        _flag(viewer.user)
        client = self._client(viewer)

        client.get(urls[name])
        response = client.get(urls[name])

        assert response.status_code == 200
        assert response[offline.SNAPSHOT_HEADER] == "1"

    @pytest.mark.parametrize("name", ALLOWLIST)
    def test_flag_off_sends_nothing(self, name):
        viewer, urls = _allowlisted_urls()
        _flag(viewer.user, enabled=False)

        response = self._client(viewer).get(urls[name])

        assert response.status_code == 200
        assert offline.SNAPSHOT_HEADER not in response
        assert offline.IDENTITY_HEADER not in response
        assert not response.has_header("Cache-Control")

    @pytest.mark.parametrize("name", ALLOWLIST)
    def test_htmx_get_sends_nothing(self, name):
        viewer, urls = _allowlisted_urls()
        _flag(viewer.user)

        response = self._client(viewer).get(urls[name], headers={"HX-Request": "true"})

        assert response.status_code == 200
        assert offline.SNAPSHOT_HEADER not in response

    @pytest.mark.parametrize("name", ALLOWLIST)
    def test_anonymous_is_redirected_without_the_header(self, name, settings):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, FLAG: True}
        _, urls = _allowlisted_urls()

        response = Client().get(urls[name])

        assert response.status_code == 302
        assert offline.SNAPSHOT_HEADER not in response

    def test_a_prefetch_keeps_its_short_lifetime(self):
        # KAN-124's tap-intent prefetch must still be reusable on an
        # opted-in page: `private` alone is not a freshness decision.
        viewer, urls = _allowlisted_urls()
        _flag(viewer.user)

        response = self._client(viewer).get(urls["pingpong:dashboard"], headers={"Purpose": "prefetch"})

        assert response["Cache-Control"] == "private, max-age=5"
        assert response[offline.SNAPSHOT_HEADER] == "1"

    @pytest.mark.parametrize("name", ["pingpong:match_add", "pingpong:play", "pingpong:player_list"])
    def test_a_page_outside_the_allowlist_sends_nothing(self, name):
        viewer = PlayerFactory(with_user=True)
        _flag(viewer.user)

        response = self._client(viewer).get(reverse(name))

        assert response.status_code == 200
        assert offline.SNAPSHOT_HEADER not in response


# ---------------------------------------------------------------------------
# Nothing opts in by accident
# ---------------------------------------------------------------------------

def _walk(patterns, namespace=""):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            inner = namespace
            if entry.namespace:
                inner = f"{namespace}{entry.namespace}:"
            yield from _walk(entry.url_patterns, inner)
        elif isinstance(entry, URLPattern):
            yield (f"{namespace}{entry.name}" if entry.name else None), entry.callback


class TestUrlPatternSweep:
    def test_decorated_views_are_exactly_the_allowlist(self):
        decorated = set()
        for name, callback in _walk(get_resolver().url_patterns):
            if offline.is_snapshot_view(callback):
                assert name, f"unnamed URL pattern opts in to snapshots: {callback!r}"
                decorated.add(name)

        assert decorated == offline.SNAPSHOT_ALLOWLIST

    def test_the_walk_sees_the_whole_site(self):
        # Guard against a sweep that passes because it saw nothing.
        names = {name for name, _ in _walk(get_resolver().url_patterns)}
        assert offline.SNAPSHOT_ALLOWLIST <= names
        assert {"login", "pingpong:live_scoreboard", "pingpong:match_confirm"} <= names
        assert any(name and name.startswith("admin:") for name in names)

    @pytest.mark.parametrize("name", [
        "pingpong:live_scoreboard", "pingpong:match_confirm", "pingpong:live_join",
        "pingpong:match_add", "pingpong:match_edit", "pingpong:notification_settings",
        "login", "logout", "service_worker",
    ])
    def test_never_list(self, name):
        assert name not in offline.SNAPSHOT_ALLOWLIST


# ---------------------------------------------------------------------------
# What the page announces
# ---------------------------------------------------------------------------

def _html_tag(html):
    found = re.search(r"<html\b[^>]*>", html)
    assert found, "no <html> tag"
    return found.group(0)


def _snapshots_meta(html):
    found = re.search(r'<meta name="ttstats-snapshots" content="([^"]*)">', html)
    assert found, "no ttstats-snapshots meta on the page"
    return found.group(1)


@pytest.mark.django_db
class TestPageMarkers:
    def _page(self, enabled):
        player = PlayerFactory(with_user=True)
        _flag(player.user, enabled)
        client = Client()
        client.force_login(player.user)
        return client.get(reverse("pingpong:dashboard")).content.decode()

    @pytest.mark.parametrize("enabled", [True, False])
    def test_freshness_marker_appears_once_on_the_html_tag(self, enabled):
        # sw.js replaces the first occurrence; it must be the root element's.
        html = self._page(enabled)
        assert html.count(offline.FRESHNESS_MARKER) == 1
        assert offline.FRESHNESS_MARKER in _html_tag(html)

    def test_rendered_at_is_an_iso_utc_timestamp(self):
        tag = _html_tag(self._page(True))
        found = re.search(r'data-rendered-at="(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"', tag)
        assert found, tag

    def test_snapshots_meta_on(self):
        assert _snapshots_meta(self._page(True)) == "on"

    def test_snapshots_meta_off(self):
        assert _snapshots_meta(self._page(False)) == "off"

    def test_logged_out_pages_announce_off(self, settings):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, FLAG: True}
        html = Client().get(reverse("login")).content.decode()
        assert _snapshots_meta(html) == "off"


# ---------------------------------------------------------------------------
# The "saved copy" banner (KAN-145): what sw.js rewrites, rendered exactly
# ---------------------------------------------------------------------------

def _inline_head_style(html):
    found = re.search(r"<head>.*?<style>(.*?)</style>", html, re.S)
    assert found, "no inline <style> in <head>"
    return found.group(1)


@pytest.mark.django_db
class TestSnapshotBanner:
    """pytest sees the markup; e2e (offline-snapshots.pwa.spec.js and the
    .nojs spec) sees whether it is on screen."""

    def _page(self, enabled=True):
        return TestPageMarkers()._page(enabled)

    def test_time_placeholder_constant_is_the_exact_element(self):
        assert offline.SNAPSHOT_TIME_PLACEHOLDER == "<time data-snapshot-time></time>"

    @pytest.mark.parametrize("enabled", [True, False])
    def test_banner_and_placeholder_render_once_on_every_page(self, enabled):
        # Flag off too: a copy stored before the kill switch still needs it.
        html = self._page(enabled)
        assert html.count(offline.SNAPSHOT_TIME_PLACEHOLDER) == 1
        assert html.count("data-snapshot-banner") == 1
        assert 'class="snapshot-banner ' in html

    def test_placeholder_sits_inside_the_banner_after_the_marker(self):
        html = self._page()
        banner = html.index("data-snapshot-banner")
        assert html.index(offline.FRESHNESS_MARKER) < banner
        assert banner < html.index(offline.SNAPSHOT_TIME_PLACEHOLDER)

    def test_both_wordings_are_present(self):
        html = self._page()
        assert "No connection — saved copy from " in html
        assert "Saved copy from " in html
        assert " — may be out of date" in html

    def test_banner_carries_the_wifi_off_glyph(self):
        html = self._page()
        start = html.index("data-snapshot-banner")
        assert '<use href="#i-wifi-off">' in html[start:start + 1500]
        assert 'id="i-wifi-off"' in html  # the sprite has the symbol

    def test_hide_and_show_rules_are_inlined_in_head(self):
        # Fail closed without the stylesheet, both ways (frontend.md).
        style = _inline_head_style(self._page())
        assert ".snapshot-banner,.snapshot-old{display:none}" in style
        assert 'html[data-freshness="snapshot"] .snapshot-banner{display:block}' in style
        assert 'html[data-snapshot-age="old"] .snapshot-old{display:inline}' in style
        assert 'html[data-snapshot-age="old"] .snapshot-recent{display:none}' in style

    def test_banner_is_not_amber_or_red(self):
        html = self._page()
        start = html.index("data-snapshot-banner")
        banner = html[html.rindex("<div", 0, start):html.index("</div>", start)]
        for forbidden in ("ball", "paddle", "live-dot", "amber", "red-"):
            assert forbidden not in banner, forbidden

    def test_worker_looks_for_the_same_strings(self, client):
        body = client.get("/sw.js").content.decode()
        assert f"const FRESHNESS_MARKER = '{offline.FRESHNESS_MARKER}';" in body
        assert f"const TIME_PLACEHOLDER = '{offline.SNAPSHOT_TIME_PLACEHOLDER}';" in body
        assert f"const SNAPSHOT_HEADER = '{offline.SNAPSHOT_HEADER}';" in body
        assert f"const IDENTITY_HEADER = '{offline.IDENTITY_HEADER}';" in body

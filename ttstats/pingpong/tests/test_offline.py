"""The server side of the service worker's offline scoreboard cache (KAN-82).

pytest cannot run the worker; the e2e `iphone-pwa` project does
(`tests/e2e/scoreboard-offline.pwa.spec.js`). These pin what Python owns:
the precache list, the identity fingerprint, and where it is sent.
"""
import re
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse

from pingpong import live_scoring as ls
from pingpong import offline

from .conftest import MatchFactory, PlayerFactory

REPO_ROOT = Path(__file__).resolve().parents[3]
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _live_match():
    p1 = PlayerFactory(with_user=True)
    p2 = PlayerFactory(with_user=True)
    match = MatchFactory(
        player1=p1, player2=p2, best_of=5, is_live=True, scorekeeper=p1,
        live_state=ls.initial_state(5),
    )
    return match, p1, p2


def _meta_identity(html: str) -> str:
    found = re.search(r'<meta name="ttstats-identity" content="([^"]*)">', html)
    assert found, "no ttstats-identity meta on the page"
    return found.group(1)


class TestIdentityFingerprint:
    def test_is_sixteen_hex_and_stable(self):
        first = offline.identity_fingerprint(7, 3)
        assert HEX16.match(first)
        assert offline.identity_fingerprint(7, 3) == first

    def test_differs_across_users_and_clubs(self):
        assert offline.identity_fingerprint(7, 3) != offline.identity_fingerprint(8, 3)
        assert offline.identity_fingerprint(7, 3) != offline.identity_fingerprint(7, 4)
        assert offline.identity_fingerprint(7, None) != offline.identity_fingerprint(7, 3)

    def test_is_not_the_raw_id(self):
        assert not offline.identity_fingerprint(7, 3).startswith("7:")


class TestPrecacheList:
    def test_every_entry_exists_or_is_a_build_output(self):
        # A path missing from the manifest would 500 /sw.js in production.
        # Vendored JS, fonts and the compiled CSS are gitignored build
        # outputs (`npm run build`), so a fresh checkout may not have them.
        vendor = (REPO_ROOT / "scripts" / "vendor_assets.mjs").read_text()
        for path in offline.SW_PRECACHE:
            built = path == "pingpong/css/app.css" or Path(path).name in vendor
            assert finders.find(path) or built, f"{path} is not a static file"

    def test_includes_what_the_scoreboard_boots_with(self):
        for path in (
            "pingpong/css/app.css",
            "pingpong/js/vendor/alpine.min.js",
            "pingpong/js/live_rules.js",
            "pingpong/js/offline.js",
        ):
            assert path in offline.SW_PRECACHE

    def test_build_key_changes_with_any_url(self):
        urls = offline.precache_urls()
        changed = [urls[0].replace(".css", ".0123456789ab.css")] + urls[1:]
        assert offline.build_key(urls) == offline.build_key(list(urls))
        assert offline.build_key(urls) != offline.build_key(changed)


@pytest.mark.django_db
class TestRequestIdentity:
    def test_anonymous_pages_announce_an_empty_identity(self, client):
        # The login page's empty identity is what tells the worker to drop
        # the last person's scoreboards.
        html = client.get(reverse("login")).content.decode()

        assert _meta_identity(html) == ""
        assert "pingpong/js/offline.js" in html

    def test_logged_in_pages_announce_the_viewers_fingerprint(self, client):
        player = PlayerFactory(with_user=True)
        client.force_login(player.user)

        html = client.get(reverse("pingpong:dashboard")).content.decode()

        assert HEX16.match(_meta_identity(html))
        assert "pingpong/js/offline.js" in html

    def test_two_users_announce_different_identities(self):
        identities = set()
        for _ in range(2):
            player = PlayerFactory(with_user=True)
            client = Client()
            client.force_login(player.user)
            identities.add(_meta_identity(client.get(reverse("pingpong:dashboard")).content.decode()))
        assert len(identities) == 2

    def test_logout_form_asks_the_worker_to_purge(self, client):
        player = PlayerFactory(with_user=True)
        client.force_login(player.user)

        html = client.get(reverse("pingpong:dashboard")).content.decode()

        assert re.search(r'<form method="post" action="/accounts/logout/" data-sw-purge>', html)


@pytest.mark.django_db
class TestScoreboardIdentityHeader:
    def test_scoreboard_carries_the_same_identity_as_its_meta(self, client):
        # The worker files the stored page under this header; the page later
        # announces the meta. They must agree or the copy deletes itself.
        match, p1, _ = _live_match()
        client.force_login(p1.user)

        response = client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))

        assert response.status_code == 200
        identity = response[offline.IDENTITY_HEADER]
        assert HEX16.match(identity)
        assert identity == _meta_identity(response.content.decode())

    def test_finished_match_redirects_without_the_header(self, client):
        # The redirect is what makes the worker evict its copy.
        match, p1, _ = _live_match()
        match.__class__.all_objects.filter(pk=match.pk).update(is_live=False)
        client.force_login(p1.user)

        response = client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))

        assert response.status_code == 302
        assert offline.IDENTITY_HEADER not in response

    def test_no_other_page_sends_the_header(self, client):
        player = PlayerFactory(with_user=True)
        client.force_login(player.user)

        for name in ("pingpong:dashboard", "pingpong:leaderboard"):
            assert offline.IDENTITY_HEADER not in client.get(reverse(name))

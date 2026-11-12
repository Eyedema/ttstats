"""The offline fallback page (KAN-137, S12.6).

The worker precaches `/offline/` at install, with no cookies, and shows it in
place of a /pingpong/ page that got no answer and has no saved copy. So the
page must be the same bytes for everyone: no user, no club, no CSRF token, no
identity meta (offline.js would read an empty one as "logged out" and purge
every saved copy). pytest pins the markup; whether it is shown, and lists the
right pages, is tests/e2e/offline-page.pwa.spec.js and tests/js/.
"""
import re

import pytest
from django.test import Client
from django.urls import reverse

from pingpong import offline

from .conftest import PlayerFactory

OFFLINE_URL = "/offline/"


def _get(client):
    response = client.get(OFFLINE_URL)
    assert response.status_code == 200
    return response


@pytest.mark.django_db
class TestOfflinePageView:
    def test_url_is_at_the_root_outside_pingpong(self):
        # Under /pingpong/ the worker's snapshot route would claim it.
        assert reverse("offline_page") == OFFLINE_URL

    def test_anonymous_gets_it_without_a_cookie(self):
        response = _get(Client())
        assert response["Content-Type"].startswith("text/html")
        assert response.cookies == {} or not any(response.cookies.values())
        assert "no-cache" in response["Cache-Control"]
        assert offline.SNAPSHOT_HEADER not in response
        assert offline.IDENTITY_HEADER not in response

    def test_same_bytes_logged_in_and_out(self):
        player = PlayerFactory(name="Zebedee Unique", with_user=True)
        client = Client()
        client.force_login(player.user)
        logged_in = _get(client).content.decode()
        anonymous = _get(Client()).content.decode()

        assert logged_in == anonymous
        assert player.user.username not in logged_in
        assert "Zebedee Unique" not in logged_in

    def test_no_csrf_token_and_no_identity_meta(self):
        player = PlayerFactory(with_user=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(player.user)
        html = _get(client).content.decode()

        assert "csrfmiddlewaretoken" not in html
        assert "X-CSRFToken" not in html
        assert "ttstats-identity" not in html
        assert "ttstats-snapshots" not in html
        assert "<form" not in html

    def test_view_matches_the_digested_html(self):
        assert _get(Client()).content.decode() == offline.offline_page_html()


class TestOfflinePageMarkup:
    @pytest.fixture
    def html(self):
        return offline.offline_page_html()

    def test_reason_marker_once_on_html(self, html):
        assert offline.OFFLINE_REASON_MARKER == 'data-offline-reason="network"'
        assert html.count(offline.OFFLINE_REASON_MARKER) == 1
        tag = re.search(r"<html[^>]*>", html).group(0)
        assert offline.OFFLINE_REASON_MARKER in tag

    def test_the_ticket_sentence(self, html):
        assert "No connection</span>" in html
        assert ", and this page isn&#x27;t saved on your phone yet." in html or (
            ", and this page isn't saved on your phone yet." in html
        )

    def test_server_wording_is_hidden_unless_rewritten(self, html):
        style = re.search(r"<head>.*?<style>(.*?)</style>", html, re.S).group(1)
        assert ".offline-server{display:none}" in style
        assert 'html[data-offline-reason="server"] .offline-server{display:inline}' in style
        assert 'html[data-offline-reason="server"] .offline-network{display:none}' in style

    def test_no_js_links_to_today_and_the_table(self, html):
        static = html[html.index("data-offline-static"):]
        assert f'href="{reverse("pingpong:dashboard")}"' in static
        assert f'href="{reverse("pingpong:leaderboard")}"' in static

    def test_try_again_reloads_this_url_without_js(self, html):
        assert re.search(r'<a href="" class="btn-ghost[^"]*" data-offline-retry>', html)

    def test_js_managed_parts_start_hidden(self, html):
        assert re.search(r"<div[^>]*data-offline-saved hidden>", html)
        assert re.search(r"<p[^>]*data-offline-empty hidden>", html)

    def test_loads_its_script_and_the_stylesheet_no_inline_script(self, html):
        assert 'src="/static/pingpong/js/offline_page.js"' in html
        assert "pingpong/css/app.css" in html
        assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html) is None

    def test_icons_come_from_its_own_sprite(self, html):
        for name in ("wifi-off", "refresh-cw", "chevron-right"):
            assert f'<use href="#i-{name}">' in html
            assert f'id="i-{name}"' in html

    def test_not_amber_or_red(self, html):
        body = html[html.index("<main"):]
        for forbidden in ("ball", "paddle", "live-dot", "amber", "red-", "btn-paddle"):
            assert forbidden not in body, forbidden

    def test_precached_assets(self):
        assert "pingpong/js/offline_page.js" in offline.SW_PRECACHE


@pytest.mark.django_db
class TestWorkerKnowsThePage:
    def test_worker_constants(self, client):
        body = client.get("/sw.js").content.decode()
        assert "const OFFLINE_URL = '/offline/';" in body
        assert f"const OFFLINE_REASON_MARKER = '{offline.OFFLINE_REASON_MARKER}';" in body

    def test_editing_the_page_changes_the_build(self, client, monkeypatch):
        """Its URL has no content hash, so its bytes are in BUILD: an edited
        template reinstalls the worker, which re-fetches the page."""
        urls = offline.precache_urls()
        before = offline.build_key(urls, offline.offline_page_html())
        assert f"const BUILD = VERSION + '-{before}';" in client.get("/sw.js").content.decode()

        monkeypatch.setattr(offline, "offline_page_html", lambda: "<html>edited</html>")
        after = offline.build_key(urls, "<html>edited</html>")
        assert after != before
        assert f"const BUILD = VERSION + '-{after}';" in client.get("/sw.js").content.decode()

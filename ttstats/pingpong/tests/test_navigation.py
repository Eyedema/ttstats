"""The navigation spine: the tab bar, the tiered drawer, and the icon sprite.

Nine identical sidebar links became four tab destinations plus a drawer. The
things worth protecting are that the active tab is derived rather than guessed,
that the drawer renders CLOSED with no JavaScript, and that the icon sprite
actually contains every icon a template asks for -- a missing symbol renders an
empty box and reports nothing anywhere.
"""
import re

import pytest
from django.template import Context, Template
from django.test import RequestFactory
from django.urls import reverse

from pingpong.context_processors import TAB_FOR_URL_NAME, pingpong_context
from pingpong.templatetags.nav_tags import nav_active

from .conftest import PlayerFactory, UserFactory
from .test_views import _login_client, _verified_user_with_player

SPRITE = "pingpong/templates/pingpong/_icons.html"


# ---------------------------------------------------------------------------
# Active-tab resolution
# ---------------------------------------------------------------------------

class TestNavActive:
    """`nav_active` is a tag rather than an inline comparison for a reason.

    Django's `{% include with %}` cannot evaluate `active=nav_tab == 'today'`:
    it passes the *string*, which is truthy, and every row in the menu lights
    up at once. That failure produces no error, so the comparison lives here
    where it can be asserted.
    """

    def _context(self, url_name=None, nav_tab=None):
        request = RequestFactory().get("/")
        if url_name is not None:
            request.resolver_match = type("R", (), {"url_name": url_name})()
        return {"request": request, "nav_tab": nav_tab}

    def test_matches_its_own_tab(self):
        assert nav_active(self._context(nav_tab="today"), match_tab="today") is True

    def test_does_not_match_another_tab(self):
        assert nav_active(self._context(nav_tab="today"), match_tab="table") is False

    def test_matches_a_named_url(self):
        ctx = self._context(url_name="calendar")
        assert nav_active(ctx, match_urls="calendar scheduled_match_detail") is True

    def test_does_not_match_a_url_outside_its_list(self):
        ctx = self._context(url_name="leaderboard")
        assert nav_active(ctx, match_urls="calendar scheduled_match_detail") is False

    def test_a_url_name_is_matched_whole(self):
        """`match` must not light up the row that claims `match_list`."""
        ctx = self._context(url_name="match")
        assert nav_active(ctx, match_urls="match_list match_detail") is False

    def test_no_arguments_never_matches(self):
        """A row with no claim -- Admin -- highlights nothing, ever."""
        assert nav_active(self._context(url_name="dashboard")) is False

    def test_survives_a_request_with_no_resolver(self):
        request = RequestFactory().get("/")
        assert nav_active({"request": request}, match_urls="calendar") is False


class TestTabMapping:
    def test_every_mapped_tab_is_one_of_the_four(self):
        assert set(TAB_FOR_URL_NAME.values()) == {"today", "play", "table", "cups"}

    def test_starting_a_match_stays_on_play(self):
        """Play owns its whole subsystem, not just its landing page.

        Walking from Play into the match form and on into the live scoreboard
        must not silently unhighlight the tab you came from.
        """
        for url_name in ("play", "match_add", "match_schedule", "game_add", "live_scoreboard"):
            assert TAB_FOR_URL_NAME[url_name] == "play"

    def test_drawer_destinations_claim_no_tab(self):
        """Claiming a tab the user did not tap is worse than no selection."""
        for url_name in ("calendar", "head_to_head", "player_list", "match_list", "passkey_management"):
            assert url_name not in TAB_FOR_URL_NAME


@pytest.mark.django_db
class TestNavContext:
    def test_anonymous_request_still_resolves_the_tab(self):
        """The context processor runs on every render, logged in or not."""
        request = RequestFactory().get(reverse("pingpong:leaderboard"))
        request.user = type("U", (), {"is_authenticated": False})()
        request.resolver_match = type("R", (), {"url_name": "leaderboard"})()

        ctx = pingpong_context(request)
        assert ctx["nav_tab"] == "table"
        assert ctx["nav_player"] is None
        assert ctx["pending_matches_count"] == 0

    def test_user_without_a_player_does_not_crash(self):
        user = UserFactory()
        request = RequestFactory().get("/")
        request.user = user
        request.resolver_match = type("R", (), {"url_name": "dashboard"})()

        ctx = pingpong_context(request)
        assert ctx["nav_player"] is None
        assert ctx["nav_tab"] == "today"

    def test_rank_counts_players_above_you(self):
        me = PlayerFactory(with_user=True, name="Me")
        me.elo_rating = 1500
        me.save()
        for rating in (1700, 1600):
            other = PlayerFactory()
            other.elo_rating = rating
            other.save()

        request = RequestFactory().get("/")
        request.user = me.user
        request.resolver_match = type("R", (), {"url_name": "dashboard"})()

        assert pingpong_context(request)["nav_player_rank"] == 3


# ---------------------------------------------------------------------------
# Rendered chrome
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestRenderedNavigation:
    def test_tab_bar_lists_the_four_destinations(self):
        user, _ = _verified_user_with_player()
        body = _login_client(user).get(reverse("pingpong:dashboard")).content.decode()

        for label in ("Today", "Play", "Table", "Cups"):
            assert f">{label}</span>" in body

    def test_the_current_tab_is_marked_for_assistive_tech(self):
        """Fill and a red edge say "you are here" only to people who can see."""
        user, _ = _verified_user_with_player()
        body = _login_client(user).get(reverse("pingpong:leaderboard")).content.decode()
        assert 'aria-current="page"' in body

    def test_exactly_one_tab_is_current(self):
        user, _ = _verified_user_with_player()
        body = _login_client(user).get(reverse("pingpong:dashboard")).content.decode()
        # One in the tab bar, one in the drawer, one in the desktop sidebar --
        # all three render the same "Today" row from the same partials.
        tab_bar = body.split('aria-label="Primary"')[1].split("</nav>")[0]
        assert tab_bar.count('aria-current="page"') == 1

    def test_drawer_renders_closed(self):
        """Fail closed. `.overlay` without `.is-open` is display:none, and that
        rule is inlined in <head> so it cannot be lost to a bad CSS build.

        A drawer that fails open covers the viewport with its own dismiss
        control inside it, and the user is stuck. This shipped once.
        """
        user, _ = _verified_user_with_player()
        body = _login_client(user).get(reverse("pingpong:dashboard")).content.decode()

        scrim = re.search(r'<div id="mobile-menu-scrim" class="([^"]*)"', body)
        assert scrim, "drawer scrim not found"
        assert "overlay" in scrim.group(1).split()
        assert "is-open" not in scrim.group(1)
        assert ".overlay{display:none}" in body.replace(" ", "")

    def test_drawer_uses_no_framework_expressions(self):
        """Production's CSP has no 'unsafe-eval'; Alpine compiles every
        expression with new Function(). An Alpine drawer is therefore dead on
        the live site while working perfectly in dev -- which is exactly what
        shipped once.
        """
        user, _ = _verified_user_with_player()
        body = _login_client(user).get(reverse("pingpong:dashboard")).content.decode()

        drawer = body.split('id="mobile-menu-scrim"')[1].split("</body>")[0]
        for attr in ("x-data", "x-show", "@click", "x-on:click"):
            assert attr not in drawer

    def test_play_is_absent_from_the_drawer_menu(self):
        """Play is a button, not a place you browse to from a menu."""
        user, _ = _verified_user_with_player()
        body = _login_client(user).get(reverse("pingpong:dashboard")).content.decode()
        drawer = body.split('id="mobile-menu-panel"')[1].split("</body>")[0]
        assert reverse("pingpong:play") not in drawer


# ---------------------------------------------------------------------------
# The icon sprite
# ---------------------------------------------------------------------------

class TestIconSprite:
    """Icons are inlined <symbol>s referenced by <use>, not <img> tags.

    A Lucide file declares stroke="currentColor", and an <img> is its own
    document with no inherited colour -- so every icon in the app resolved to
    black, which was survivable on white and is invisible on navy.
    """

    def _sprite(self):
        from django.conf import settings
        import pathlib
        root = pathlib.Path(settings.BASE_DIR)
        return (root / "pingpong/templates/pingpong/_icons.html").read_text()

    def test_tag_emits_a_use_reference(self):
        out = Template('{% load icon_tags %}{% icon "menu" %}').render(Context({}))
        assert 'href="#i-menu"' in out
        assert 'aria-hidden="true"' in out

    def test_a_labelled_icon_is_exposed_to_assistive_tech(self):
        out = Template('{% load icon_tags %}{% icon "menu" label="Open menu" %}').render(Context({}))
        assert 'role="img"' in out
        assert 'aria-label="Open menu"' in out
        assert "aria-hidden" not in out

    def test_size_and_css_combine(self):
        out = Template('{% load icon_tags %}{% icon "menu" size="lg" css="text-ball" %}').render(Context({}))
        assert "w-6 h-6" in out
        assert "text-ball" in out

    def test_an_unknown_size_adds_no_dimensions(self):
        """A call site that states its own w-/h- must not also get a default."""
        out = Template('{% load icon_tags %}{% icon "menu" css="w-3 h-3" %}').render(Context({}))
        assert "w-5 h-5" not in out
        assert "w-3 h-3" in out

    def test_symbols_inherit_colour(self):
        sprite = self._sprite()
        assert 'stroke="currentColor"' in sprite
        # An <img> would have made this impossible: that is the whole point.
        assert "<img" not in sprite

    def test_every_icon_a_template_asks_for_is_in_the_sheet(self):
        """A symbol that is not in the sheet renders an empty box, silently.

        This is the guard that the committed sprite has been rebuilt after
        somebody added an `{% icon %}` call -- there is no runtime error to
        notice, and nothing in a screenshot-free test suite would see it.
        """
        import pathlib
        from django.conf import settings

        root = pathlib.Path(settings.BASE_DIR)
        sprite = self._sprite()
        defined = set(re.findall(r'id="i-([a-z0-9-]+)"', sprite))

        used = set()
        for path in (root / "pingpong/templates").rglob("*.html"):
            # Literal names only. `{% icon item.achievement.icon %}` is chosen
            # by data, and those names are covered by the next test.
            used |= set(re.findall(r'\{%\s*icon\s+"([a-z0-9-]+)"', path.read_text()))

        assert used <= defined, f"missing from the sprite: {sorted(used - defined)}"

    def test_every_achievement_icon_is_in_the_sheet(self):
        """Achievement glyphs are chosen by data, not by scanning templates.

        achievement_definitions.py stores a Lucide name per achievement, so
        they cannot be discovered from the markup and have to be listed in the
        sprite build script by hand.
        """
        from pingpong.achievement_definitions import ACHIEVEMENT_DEFINITIONS

        defined = set(re.findall(r'id="i-([a-z0-9-]+)"', self._sprite()))
        wanted = {a["icon"] for a in ACHIEVEMENT_DEFINITIONS}
        assert wanted <= defined, f"missing from the sprite: {sorted(wanted - defined)}"

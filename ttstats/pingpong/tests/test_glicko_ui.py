"""The Glicko-2 UI, chunk B (KAN-71 templates, KAN-72 explainer).

The promise pinned here is the no-mixing rule, on the RENDERED page: a Glicko
viewer never sees the word "Elo" and every rating component says which system
it is in; an Elo viewer never sees a "±". It is swept over every surface that
shows a rating, so a template that forgets the switch fails here, not in prod.
"""

import re
from types import SimpleNamespace

import pytest
from django.urls import reverse

from pingpong import ratings
from pingpong.models import RatingChange

from .conftest import ScheduledMatchFactory, confirm_match
from .test_glicko_read import (
    _club_with_current_season,
    _finished,
    _glicko_row,
    _member,
    glicko_on,  # noqa: F401  (fixture)
)
from .test_views import _login_client

ELO_WORD = re.compile(r"\bElo\b")
HTMX = {"HTTP_HX_REQUEST": "true"}


def _world():
    """A club with a running season, two rated players, a provisional one,
    a confirmed match, a pending one and a scheduled one."""
    club, season = _club_with_current_season()
    user, me = _member(club)
    _, them = _member(club)
    _, newbie = _member(club)
    _glicko_row(me, 1540, 55, club=club, season=season, matches=30)
    _glicko_row(me, 1540, 55, club=club, matches=30)
    _glicko_row(them, 1480, 70, club=club, season=season, matches=30)
    _glicko_row(them, 1480, 70, club=club, matches=30)
    _glicko_row(newbie, 1500, 240, club=club, season=season, matches=2)
    _glicko_row(newbie, 1500, 240, club=club, matches=2)
    confirmed = _finished(me, them, club=club)
    confirm_match(confirmed)
    pending = _finished(them, me, club=club)
    scheduled = ScheduledMatchFactory(player1=me, player2=them, club=club)
    return SimpleNamespace(
        club=club, season=season, user=user, me=me, them=them, newbie=newbie,
        confirmed=confirmed, pending=pending, scheduled=scheduled,
    )


# surface name -> (url builder, extra GET kwargs)
SURFACES = {
    "leaderboard": lambda w: (reverse("pingpong:leaderboard"), {}),
    "leaderboard_htmx": lambda w: (reverse("pingpong:leaderboard"), HTMX),
    "leaderboard_doubles": lambda w: (
        reverse("pingpong:leaderboard") + "?format=doubles", {}),
    "match_pending": lambda w: (
        reverse("pingpong:match_detail", args=[w.pending.pk]), {}),
    "match_confirmed": lambda w: (
        reverse("pingpong:match_detail", args=[w.confirmed.pk]), {}),
    "dashboard": lambda w: (reverse("pingpong:dashboard"), {}),
    "player_detail": lambda w: (
        reverse("pingpong:player_detail", args=[w.me.pk]), {}),
    "player_detail_newbie": lambda w: (
        reverse("pingpong:player_detail", args=[w.newbie.pk]), {}),
    "head_to_head": lambda w: (
        reverse("pingpong:head_to_head") + f"?player1={w.me.pk}&player2={w.them.pk}", {}),
    "calendar": lambda w: (reverse("pingpong:calendar"), {}),
    "scheduled_detail": lambda w: (
        reverse("pingpong:scheduled_match_detail", args=[w.scheduled.pk]), {}),
    "nav": lambda w: (reverse("pingpong:player_list"), {}),
    "how_ratings_work": lambda w: (reverse("pingpong:how_ratings_work"), {}),
}


def _get(world, surface):
    url, extra = SURFACES[surface](world)
    client = _login_client(world.user)
    return client.get(url, **extra)


@pytest.mark.django_db
class TestNoMixingSweep:
    @pytest.mark.parametrize("surface", sorted(SURFACES))
    def test_glicko_pages_never_say_elo_and_say_which_system(
        self, glicko_on, surface  # noqa: F811
    ):
        world = _world()

        response = _get(world, surface)

        assert response.status_code == 200
        body = response.content.decode()
        assert not ELO_WORD.search(body), ELO_WORD.search(body).group(0)
        assert 'data-rating-system="glicko2"' in body

    @pytest.mark.parametrize("seasons", [False, True], ids=["legacy", "seasons"])
    @pytest.mark.parametrize(
        "surface", sorted(s for s in SURFACES if s != "how_ratings_work")
    )
    def test_elo_pages_never_show_an_uncertainty(self, settings, seasons, surface):
        settings.FEATURE_FLAGS = {
            **settings.FEATURE_FLAGS, "seasons": seasons, "glicko2": False,
        }
        world = _world()

        response = _get(world, surface)

        assert response.status_code == 200
        body = response.content.decode()
        assert "±" not in body
        assert "\\u00b1" not in body
        assert 'data-rating-system="glicko2"' not in body
        assert "give or take" not in body

    @pytest.mark.parametrize("seasons", [False, True], ids=["legacy", "seasons"])
    def test_the_explainer_404s_under_elo(self, settings, seasons):
        settings.FEATURE_FLAGS = {
            **settings.FEATURE_FLAGS, "seasons": seasons, "glicko2": False,
        }
        world = _world()

        assert _get(world, "how_ratings_work").status_code == 404

    def test_the_explainer_needs_a_login(self, glicko_on):  # noqa: F811
        from django.test import Client

        response = Client().get(reverse("pingpong:how_ratings_work"))

        assert response.status_code == 302


@pytest.mark.django_db
class TestComponents:
    def test_leaderboard_rows_show_the_uncertainty_as_text(self, glicko_on):  # noqa: F811
        body = _get(_world(), "leaderboard").content.decode()

        assert re.search(r"\d{4}\s*<span[^>]*data-rating-rd[^>]*>±\d+", body)
        assert "give or take" in body

    def test_the_fragment_and_the_page_render_the_same_rows(self, glicko_on):  # noqa: F811
        world = _world()

        page = _get(world, "leaderboard").content.decode()
        fragment = _get(world, "leaderboard_htmx").content.decode()

        assert "<html" not in fragment
        rows = lambda s: re.findall(r"data-rated-row", s)
        assert len(rows(page)) == len(rows(fragment)) == 3
        assert fragment.strip() in page.replace("\r", "")

    def test_provisional_is_a_bordered_label_never_amber(self, glicko_on):  # noqa: F811
        body = _get(_world(), "leaderboard").content.decode()

        chip = re.search(r'<span[^>]*data-rating-status="provisional"[^>]*>', body).group(0)
        assert "label-cap" in chip and "border" in chip
        assert "amber" not in chip and "ball" not in chip
        assert 'data-rating-reason="new"' in chip

    def test_a_returning_player_is_labelled_returning(self, glicko_on):  # noqa: F811
        import datetime

        from django.utils import timezone

        from pingpong.models import PlayerRating

        world = _world()
        PlayerRating.all_objects.filter(player=world.me).update(
            rd=300, last_rated_on=timezone.now().date() - datetime.timedelta(days=1)
        )

        body = _get(world, "leaderboard").content.decode()

        assert 'data-rating-reason="returning"' in body
        assert ">Returning<" in body

    def test_nav_shows_the_player_reading(self, glicko_on):  # noqa: F811
        body = _get(_world(), "nav").content.decode()

        assert re.search(r"data-nav-rating[^>]*>\s*15\d\d\s*<span[^>]*>±\d+", body)

    def test_pending_delta_has_no_unit_and_says_if_agreed(self, glicko_on):  # noqa: F811
        body = _get(_world(), "match_pending").content.decode()

        assert re.search(r"data-rating-delta[^>]*>\s*[+-]\d+\s*<span[^>]*>if agreed", body)

    def test_dashboard_wording_is_neutral(self, glicko_on):  # noqa: F811
        body = _get(_world(), "dashboard").content.decode()

        assert re.search(r"[+-]\d+</span> if true", body)

    def test_the_help_links_to_the_explainer(self, glicko_on):  # noqa: F811
        body = _get(_world(), "leaderboard").content.decode()

        assert reverse("pingpong:how_ratings_work") in body
        assert "data-how-ratings-link" in body

    def test_the_nav_menu_links_the_explainer_only_under_glicko(self, glicko_on, settings):  # noqa: F811
        world = _world()
        assert "How ratings work" in _get(world, "nav").content.decode()

        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "glicko2": False}
        assert "How ratings work" not in _get(world, "nav").content.decode()

    def test_player_detail_draws_a_band_chart(self, glicko_on):  # noqa: F811
        world = _world()

        body = _get(world, "player_detail").content.decode()

        assert 'id="ratingChart"' in body
        assert 'id="rating-chart-upper"' in body
        assert 'id="eloChart"' not in body

    def test_achievement_copy_is_neutral_under_glicko_only(self, glicko_on, settings):  # noqa: F811
        world = _world()
        glicko = _get(world, "player_detail").content.decode()
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "glicko2": False}
        elo = _get(world, "player_detail").content.decode()

        assert "points above you" in glicko
        assert "Elo above you" in elo


@pytest.mark.django_db
class TestPreviewConfirmWrittenIntegration:
    """Happy path: the number promised before agreeing is the number written."""

    def test_the_pending_figure_is_the_written_one(self, glicko_on):  # noqa: F811
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1450, 80, club=club, season=season)
        _glicko_row(them, 1500, 80, club=club, season=season)
        match = _finished(them, me, club=club)
        client = _login_client(user)

        pending_page = client.get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).content.decode()
        on_dashboard = client.get(reverse("pingpong:dashboard")).context[
            "pending_confirmations"
        ][0]["elo_if_true"]
        confirm_match(match)
        confirmed_page = client.get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).content.decode()

        written = RatingChange.objects.get(
            match=match, player=me, rating__season=season
        ).glicko_change
        delta = r"data-rating-delta[^>]*>\s*([+-]\d+)"
        previewed = [int(n) for n in re.findall(delta, pending_page)]
        assert -written in previewed or written in previewed
        assert on_dashboard == written
        assert "if agreed" in pending_page
        assert "if agreed" not in confirmed_page
        assert f"{written:+d}" in confirmed_page
        assert not ELO_WORD.search(pending_page + confirmed_page)
        assert ratings.system(user) == ratings.GLICKO

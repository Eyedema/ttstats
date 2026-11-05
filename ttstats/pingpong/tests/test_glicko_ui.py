"""The Glicko-2 UI, chunk B (KAN-71 templates, KAN-72 explainer).

The promise pinned here is the no-mixing rule, on the RENDERED page: a Glicko
viewer never sees the word "Elo" and every rating component says which system
it is in; an Elo viewer never sees a "±". It is swept over every HTML surface
that shows a rating, so a template that forgets the switch fails here, not in
prod. The push text is not a page; it is covered in test_glicko_read.
"""

import re
from types import SimpleNamespace

import pytest
from django.urls import reverse

from pingpong import ratings
from pingpong.models import RatingChange

from .conftest import (
    GameFactory,
    MatchFactory,
    ScheduledMatchFactory,
    SeasonStandingFactory,
    confirm_match,
)
from .test_glicko_read import (
    _club_with_current_season,
    _finished,
    _glicko_row,
    _member,
    glicko_on,  # noqa: F401  (fixture)
)
from .test_views import _login_client

ELO_WORD = re.compile(r"\belo\b", re.I)
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
    doubles_pending, doubles_confirmed = _doubles(club, season, me, them)
    archived_glicko, archived_elo = _archives(club, me, them)
    return SimpleNamespace(
        club=club, season=season, user=user, me=me, them=them, newbie=newbie,
        confirmed=confirmed, pending=pending, scheduled=scheduled,
        doubles_pending=doubles_pending, doubles_confirmed=doubles_confirmed,
        archived_glicko=archived_glicko, archived_elo=archived_elo,
    )


def _doubles(club, season, me, them):
    """A pending and a confirmed doubles match between four rated players."""
    others = [_member(club)[1] for _ in range(2)]
    for p, rating, rd in zip(
        (me, them, *others), (1560, 1490, 1420, 1510), (60, 90, 140, 75)
    ):
        _glicko_row(p, rating, rd, club=club, season=season, fmt="doubles", matches=20)
    out = []
    # Confirmed first: the pending match is the later one, so confirming it
    # later writes from the very state the preview read (no out-of-order replay).
    for confirm in (True, False):
        match = MatchFactory(
            team1_players=[me, others[0]], team2_players=[them, others[1]],
            is_double=True, best_of=5, club=club,
        )
        for n in (1, 2, 3):
            GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)
        match.refresh_from_db()
        if confirm:
            confirm_match(match)
        out.append(match)
    confirmed, pending = out
    return pending, confirmed


def _archives(club, me, them):
    """One archived season frozen under Glicko-2 (with a +/-), one under Elo."""
    import datetime

    from django.utils import timezone

    from .conftest import SeasonFactory

    today = timezone.now().date()
    made = []
    for offset, system, rd in ((400, "glicko2", 61.4), (300, "elo", None)):
        archived = SeasonFactory(
            club=club, name="Winter" if system == "glicko2" else "Spring",
            starts_on=today - datetime.timedelta(days=offset),
            ends_on=today - datetime.timedelta(days=offset - 50),
            archived_at=timezone.now(),
        )
        SeasonStandingFactory(
            season=archived, player=me, rank=1, rating=1555,
            rating_system=system, rd=rd,
        )
        SeasonStandingFactory(
            season=archived, player=them, rank=2, rating=1480,
            rating_system=system, rd=rd,
        )
        made.append(archived)
    return made


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
    "match_doubles_pending": lambda w: (
        reverse("pingpong:match_detail", args=[w.doubles_pending.pk]), {}),
    "match_doubles_confirmed": lambda w: (
        reverse("pingpong:match_detail", args=[w.doubles_confirmed.pk]), {}),
    "archive_glicko_frozen": lambda w: (
        reverse("pingpong:season_archive_detail", args=[w.archived_glicko.pk]), {}),
    "archive_elo_frozen": lambda w: (
        reverse("pingpong:season_archive_detail", args=[w.archived_elo.pk]), {}),
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
        # A frozen Elo standing keeps its machine-readable system token; that
        # attribute is not words on the page.
        body = body.replace('data-rating-system="elo"', "")
        found = ELO_WORD.search(body)
        assert not found, body[max(found.start() - 80, 0):found.end() + 80] if found else ""
        assert 'data-rating-system="glicko2"' in body

    @pytest.mark.parametrize("seasons", [False, True], ids=["legacy", "seasons"])
    @pytest.mark.parametrize(
        "surface",
        sorted(s for s in SURFACES if s not in ("how_ratings_work", "archive_glicko_frozen")),
    )
    def test_elo_pages_never_show_an_uncertainty(self, settings, seasons, surface):
        settings.FEATURE_FLAGS = {
            **settings.FEATURE_FLAGS, "seasons": seasons, "glicko2": False,
        }
        world = _world()

        response = _get(world, surface)

        # The archive is a seasons page: it 404s with the flag off.
        expected = 404 if surface.startswith("archive") and not seasons else 200
        assert response.status_code == expected
        if expected == 404:
            return
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
class TestFrozenStandings:
    """The archive shows what the season was frozen under, whatever the viewer has."""

    def test_a_glicko_frozen_standing_shows_its_uncertainty(self, glicko_on):  # noqa: F811
        body = _get(_world(), "archive_glicko_frozen").content.decode()

        assert re.search(r"1555\s*<span[^>]*data-rating-rd[^>]*>±61", body)
        assert "give or take 61" in body

    def test_an_elo_frozen_standing_shows_none(self, glicko_on):  # noqa: F811
        body = _get(_world(), "archive_elo_frozen").content.decode()

        # Only the standings: the nav shows the VIEWER's own (Glicko) reading.
        cells = re.findall(r'data-testid="standing-rating".*?</span>\s*</span>|data-testid="standing-rating".*?</span>', body, re.S)
        assert cells and all("1555" in c or "1480" in c for c in cells)
        assert not any("±" in c or "give or take" in c for c in cells)


@pytest.mark.django_db
class TestDoublesMatchPage:
    def _players_deltas(self, body):
        return re.findall(
            r"data-player-delta>([^<]+)</span>\s*<span[^>]*data-rating-delta[^>]*>\s*([+-]\d+)", body
        )

    @pytest.mark.parametrize("surface", ["match_doubles_pending", "match_doubles_confirmed"])
    def test_each_player_gets_their_own_figure(self, glicko_on, surface):  # noqa: F811
        body = _get(_world(), surface).content.decode()

        shown = self._players_deltas(body)
        assert len(shown) == 4
        assert len({delta for _, delta in shown}) > 1  # not one side mean, repeated

    def test_the_pending_figures_are_the_written_ones(self, glicko_on):  # noqa: F811
        world = _world()
        match = world.doubles_pending
        client = _login_client(world.user)
        url = reverse("pingpong:match_detail", args=[match.pk])

        previewed = dict(self._players_deltas(client.get(url).content.decode()))
        assert "if agreed" in client.get(url).content.decode()
        confirm_match(match)
        confirmed = dict(self._players_deltas(client.get(url).content.decode()))

        written = {
            rc.player.name: rc.glicko_change
            for rc in RatingChange.objects.filter(
                match=match, rating__season=world.season
            ).select_related("player")
        }
        assert {k: int(v) for k, v in previewed.items()} == written
        assert {k: int(v) for k, v in confirmed.items()} == written

    def test_the_context_has_no_elo_named_keys(self, glicko_on):  # noqa: F811
        world = _world()
        context = _login_client(world.user).get(
            reverse("pingpong:match_detail", args=[world.doubles_pending.pk])
        ).context

        for key in ("side1_elo_delta", "side2_elo_delta", "elo_is_pending"):
            assert key not in context


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

        assert re.search(r"[+-]\d+</span> if agreed", body)

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

        assert "well above you" in glicko
        assert "points above you" not in glicko
        assert not re.search(r"\d+\+ (Elo|points)", glicko)
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
        ][0]["delta_if_true"]
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

"""The Glicko-2 read cutover, chunk A (KAN-72 flag, KAN-71 read layer).

The contract is `docs/decisions/kan-63-cutover-plan.md`. The promises pinned
here: `ratings.system()` is a truth table, a preview is exactly what is
written (per player, every scope), the stored pre-match values feed the win
probability, the leaderboard counts a NULL Glicko row as unrated, the two
systems never share a cache key, and the writers fill the new history columns.
"""

import datetime
from datetime import date
from io import StringIO
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pingpong import cache_utils, elo, flags, glicko2, glicko_adapter, ratings
from pingpong.elo import LiveBook, RatingScope, update_player_elo
from pingpong.elo_replay import RatingWriter
from pingpong.models import (
    FeatureFlagOverride,
    NotificationKind,
    PlayerRating,
    RatingChange,
    SeasonStanding,
)

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    PlayerRatingFactory,
    SeasonFactory,
    UserFactory,
    confirm_match,
)
from .test_glicko_shadow import _at, _finish, _pending, _season
from .test_views import _login_client

HISTORY = glicko_adapter.HISTORY_FIELDS
LEADERBOARD = "pingpong:leaderboard"


@pytest.fixture
def glicko_on(settings):
    settings.FEATURE_FLAGS = {
        **settings.FEATURE_FLAGS,
        "seasons": True,
        "glicko2": True,
    }


def _member(club, **kwargs):
    user = UserFactory()
    user.profile.email_verified = True
    user.profile.save()
    player = PlayerFactory(user=user, club=club, **kwargs)
    ClubMembershipFactory(club=club, user=user)
    return user, player


def _club_with_current_season():
    club = ClubFactory()
    today = timezone.now().date()
    season = SeasonFactory(
        club=club,
        name="Now",
        starts_on=today - datetime.timedelta(days=10),
        ends_on=today + datetime.timedelta(days=80),
    )
    return club, season


def _finished(winner, loser, *, club, **kwargs):
    match = MatchFactory(player1=winner, player2=loser, best_of=5, club=club, **kwargs)
    for n in (1, 2, 3):
        GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)
    match.refresh_from_db()
    return match


def _glicko_row(
    player,
    glicko,
    rd,
    *,
    club=None,
    season=None,
    fmt="singles",
    matches=5,
    last=None,
    **kwargs,
):
    return PlayerRatingFactory(
        player=player,
        club=club,
        season=season,
        format=fmt,
        matches=matches,
        glicko_rating=glicko,
        rd=rd,
        volatility=0.06,
        last_rated_on=last or timezone.now().date(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# system(): the truth table
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSystem:
    @pytest.mark.parametrize(
        "seasons, glicko2_flag, expected",
        [
            (False, False, ratings.ELO),
            (True, False, ratings.ELO),
            (False, True, ratings.ELO),
            (True, True, ratings.GLICKO),
        ],
    )
    def test_four_flag_combinations(self, settings, seasons, glicko2_flag, expected):
        settings.FEATURE_FLAGS = {
            **settings.FEATURE_FLAGS,
            "seasons": seasons,
            "glicko2": glicko2_flag,
        }
        user = UserFactory()

        assert ratings.system(user) == expected
        assert ratings.system(None) == expected

    def test_overrides_win_in_both_directions(self, settings):
        user, other = UserFactory(), UserFactory()
        FeatureFlagOverride.objects.create(name="seasons", user=user, enabled=True)
        FeatureFlagOverride.objects.create(name="glicko2", user=user, enabled=True)

        assert ratings.system(user) == ratings.GLICKO
        assert ratings.system(other) == ratings.ELO
        assert ratings.system(None) == ratings.ELO

        settings.FEATURE_FLAGS = {
            **settings.FEATURE_FLAGS,
            "seasons": True,
            "glicko2": True,
        }
        FeatureFlagOverride.objects.create(name="glicko2", user=other, enabled=False)
        assert ratings.system(other) == ratings.ELO
        assert ratings.system(UserFactory()) == ratings.GLICKO

    def test_glicko_alone_is_elo_and_the_check_warns(self, settings):
        settings.FEATURE_FLAGS = {
            **settings.FEATURE_FLAGS,
            "seasons": False,
            "glicko2": True,
        }

        assert [w.id for w in flags.check_glicko_needs_seasons()] == ["pingpong.W001"]
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "seasons": True}
        assert flags.check_glicko_needs_seasons() == []

    def test_the_names_match_the_cache_layer(self):
        assert (ratings.ELO, ratings.GLICKO) == (cache_utils.ELO, cache_utils.GLICKO)
        assert ratings.ALL_TIME == cache_utils.ALL_TIME_SEASON

    def test_context_processor_exposes_the_system(self, glicko_on, rf):
        from pingpong.context_processors import pingpong_context

        user, _ = _member(ClubFactory())
        request = rf.get("/")
        request.user = user

        ctx = pingpong_context(request)

        assert ctx["rating_system"] == ratings.GLICKO
        assert ctx["nav_rating"] is None  # unrated: no Glicko row yet


# ---------------------------------------------------------------------------
# Preview equals written
# ---------------------------------------------------------------------------


def _written(match, player, scope):
    qs = RatingChange.objects.filter(
        match=match, player=player, rating__format=scope.format
    )
    qs = (
        qs.filter(rating__season=scope.season)
        if scope.season
        else qs.filter(rating__season__isnull=True)
    )
    return qs.get()


@pytest.mark.django_db
class TestPreviewEqualsWritten:
    def _check(self, match, scopes, players):
        previews = {
            (scope, p.pk): ratings.projected_change_for(
                match, p, scope, system=ratings.GLICKO
            )
            for scope in scopes
            for p in players
        }
        side_previews = {
            scope: ratings.match_deltas(match, scope, system=ratings.GLICKO)
            for scope in scopes
        }
        confirm_match(match)
        for scope in scopes:
            for p in players:
                assert (
                    _written(match, p, scope).glicko_change == previews[(scope, p.pk)]
                )
            assert side_previews[scope][2] is True
            assert ratings.match_deltas(match, scope, system=ratings.GLICKO)[2] is False

    def test_singles_all_time_and_season(self):
        club, season = _club_with_current_season()
        _, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1620, 90, club=club)
        _glicko_row(them, 1480, 200, club=club)
        match = _finished(me, them, club=club)

        self._check(
            match,
            [RatingScope(None, "singles"), RatingScope(season, "singles")],
            [me, them],
        )

    def test_the_season_seed_when_only_an_all_time_row_exists(self):
        club, season = _club_with_current_season()
        _, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1700, 60, club=club)
        _glicko_row(them, 1300, 120, club=club)
        match = _finished(me, them, club=club)

        scope = RatingScope(season, "singles")
        preview = ratings.projected_change_for(match, me, scope, system=ratings.GLICKO)
        confirm_match(match)

        row = _written(match, me, scope)
        assert row.glicko_change == preview
        # Seeded from the all-time rating, pulled toward 1500 (not 1700).
        assert row.glicko_old_rating == pytest.approx(
            glicko2.BASE_RATING
            + 0.5 * 0
            + (1700 - 1500) * (row.glicko_old_rating - 1500) / (1700 - 1500)
        )
        assert row.glicko_old_rating < 1700

    def test_a_season_row_with_null_glicko_state_is_seeded(self):
        club, season = _club_with_current_season()
        _, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1650, 80, club=club)
        _glicko_row(them, 1500, 80, club=club)
        PlayerRatingFactory(
            player=me, club=club, season=season, matches=2
        )  # NULL shadow
        match = _finished(me, them, club=club)

        scope = RatingScope(season, "singles")
        preview = ratings.projected_change_for(match, me, scope, system=ratings.GLICKO)
        confirm_match(match)

        assert _written(match, me, scope).glicko_change == preview

    def test_doubles_per_player(self):
        club, season = _club_with_current_season()
        users = [_member(club) for _ in range(4)]
        p1, p2, p3, p4 = (p for _, p in users)
        _glicko_row(p1, 1600, 70, club=club, fmt="doubles")
        _glicko_row(p3, 1450, 150, club=club, fmt="doubles")
        match = MatchFactory(
            team1_players=[p1, p2],
            team2_players=[p3, p4],
            is_double=True,
            best_of=5,
            club=club,
        )
        for n in (1, 2, 3):
            GameFactory(match=match, game_number=n, team1_score=11, team2_score=6)
        match.refresh_from_db()

        self._check(
            match,
            [RatingScope(None, "doubles"), RatingScope(season, "doubles")],
            [p1, p2, p3, p4],
        )

    def test_history_columns_describe_the_update(self):
        match, p1, p2 = _pending()
        update_player_elo(match)

        row = RatingChange.objects.get(
            match=match, player=p1, rating__season__isnull=True
        )

        assert all(getattr(row, f) is not None for f in HISTORY)
        assert row.glicko_new_rating > row.glicko_old_rating
        assert row.glicko_change == round(row.glicko_new_rating - row.glicko_old_rating)
        assert row.glicko_rd_after < row.glicko_rd_before <= glicko2.MAX_RD

    def test_a_confirmed_match_with_null_history_applies_zero_not_a_preview(self):
        club, season = _club_with_current_season()
        _, me = _member(club)
        _, them = _member(club)
        match = _finished(me, them, club=club)
        confirm_match(match)
        scope = RatingScope(season, "singles")
        RatingChange.objects.filter(match=match).update(**{f: None for f in HISTORY})

        assert ratings.match_deltas(match, scope, system=ratings.GLICKO) == (
            0,
            0,
            False,
        )


# ---------------------------------------------------------------------------
# Win probability from stored pre-match values
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWinProbability:
    def test_confirmed_singles_uses_stored_pre_match_values(self):
        match, p1, p2 = _pending()
        scope = ratings.display_scope(match)
        update_player_elo(match)
        win = RatingChange.objects.get(
            match=match, player=p1, rating__season__isnull=True
        )
        lose = RatingChange.objects.get(
            match=match, player=p2, rating__season__isnull=True
        )
        expected = glicko2.win_probability(
            win.glicko_old_rating,
            win.glicko_rd_before,
            lose.glicko_old_rating,
            lose.glicko_rd_before,
        )

        got = ratings.win_probability(
            [p1], [p2], system=ratings.GLICKO, scope=scope, match=match
        )

        assert got == (round(expected * 100), 100 - round(expected * 100))

    def test_confirmed_doubles_uses_composite_of_stored_values(self):
        club = ClubFactory()
        ps = [PlayerFactory(club=club) for _ in range(4)]
        match = _finish(ps[:2], ps[2:], club=club, when=_at(2026, 2, 1))
        scope = ratings.display_scope(match)
        rows = {
            c.player_id: c
            for c in RatingChange.objects.filter(
                match=match, rating__season__isnull=True
            )
        }
        sides = []
        for side in (ps[:2], ps[2:]):
            sides.append(
                glicko_adapter.composite_opponent(
                    glicko2.Rating(
                        rows[p.pk].glicko_old_rating, rows[p.pk].glicko_rd_before, 0.06
                    )
                    for p in side
                )
            )
        expected = round(glicko2.win_probability(*sides[0], *sides[1]) * 100)

        got = ratings.win_probability(
            ps[:2], ps[2:], system=ratings.GLICKO, scope=scope, match=match
        )

        assert got == (expected, 100 - expected)

    def test_a_pairing_with_no_match_reads_the_live_book(self):
        club, season = _club_with_current_season()
        a, b = PlayerFactory(club=club), PlayerFactory(club=club)
        _glicko_row(a, 1800, 50, club=club)
        _glicko_row(b, 1400, 50, club=club)

        pct = ratings.win_probability(
            [a], [b], system=ratings.GLICKO, scope=RatingScope(None, "singles")
        )

        assert pct[0] > 85 and sum(pct) == 100

    def test_an_unrated_pairing_is_even(self):
        a, b = PlayerFactory(), PlayerFactory()

        assert ratings.win_probability(
            [a], [b], system=ratings.GLICKO, scope=RatingScope(None, "singles")
        ) == (50, 50)

    def test_elo_delegates_to_the_old_function(self):
        a, b = PlayerFactory(elo_rating=1700), PlayerFactory(elo_rating=1500)

        assert ratings.win_probability(
            [a], [b], system=ratings.ELO, scope=None
        ) == elo.get_win_probability([a], [b])

    def test_for_players_loads_both_scopes_in_one_query(self):
        club, season = _club_with_current_season()
        a = PlayerFactory(club=club)
        _glicko_row(a, 1550, 80, club=club)
        _glicko_row(a, 1540, 90, club=club, season=season)

        with CaptureQueriesContext(connection) as ctx:
            book = LiveBook.for_players([a.pk], season, "singles")

        assert len(ctx.captured_queries) == 1
        assert book.row(a.pk, None, "singles") and book.row(a.pk, season.pk, "singles")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_rd_threshold_is_read_at_call_time(self, monkeypatch):
        s = ratings.GLICKO
        monkeypatch.setattr(glicko_adapter, "PROVISIONAL_RD", 100.0)
        assert (
            ratings.rating_status(True, experience=50, rd=101, system=s)
            == ratings.PROVISIONAL
        )
        monkeypatch.setattr(glicko_adapter, "PROVISIONAL_RD", 120.0)
        assert (
            ratings.rating_status(True, experience=50, rd=101, system=s)
            == ratings.ESTABLISHED
        )

    def test_reason_is_new_for_few_matches_and_returning_when_idle(self):
        s = ratings.GLICKO
        new = ratings.rating_status(
            True, experience=elo.PROVISIONAL_MATCHES - 1, rd=300, system=s
        )
        back = ratings.rating_status(True, experience=40, rd=300, system=s)

        assert (new, new.reason) == (ratings.PROVISIONAL, "new")
        assert (back, back.reason) == (ratings.PROVISIONAL, "returning")
        assert (
            ratings.rating_status(True, experience=40, rd=50, system=s).reason is None
        )

    def test_unrated_and_elo_are_unchanged(self):
        assert ratings.rating_status(False, system=ratings.GLICKO) == ratings.UNRATED
        assert (
            ratings.rating_status(
                True, experience=elo.PROVISIONAL_MATCHES - 1, system=ratings.ELO
            )
            == ratings.PROVISIONAL
        )
        assert (
            ratings.rating_status(
                True, experience=elo.PROVISIONAL_MATCHES, system=ratings.ELO
            )
            == ratings.ESTABLISHED
        )

    def test_display_rd_grows_with_idle_days_and_is_capped(self):
        row = {
            "rd": 80.0,
            "volatility": 0.06,
            "glicko_rating": 1500.0,
            "last_rated_on": date(2026, 1, 1),
        }

        same = ratings.display_rd(row, date(2026, 1, 1))
        later = ratings.display_rd(row, date(2026, 6, 1))
        forever = ratings.display_rd(row, date(2100, 1, 1))

        assert same == pytest.approx(80.0)
        assert same < later <= forever <= glicko2.MAX_RD
        assert ratings.display_rd({"rd": None}, date(2026, 1, 1)) is None


# ---------------------------------------------------------------------------
# The leaderboard
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGlickoLeaderboard:
    def test_sorted_by_glicko_rating_with_neutral_keys(self):
        club, season = _club_with_current_season()
        a, b, c = (PlayerFactory(club=club) for _ in range(3))
        _glicko_row(
            a, 1480.4, 90, club=club, season=season, rating=1900
        )  # Elo disagrees
        _glicko_row(b, 1610.6, 40, club=club, season=season, rating=1400)
        _glicko_row(c, 1550.0, 120, club=club, season=season, rating=1600)

        rows, unrated = ratings.leaderboard(
            RatingScope(season, "singles"), 10, system=ratings.GLICKO
        )

        assert [r["player"] for r in rows] == [b, c, a]
        assert [r["rating"] for r in rows] == [1611, 1550, 1480]
        assert rows[0]["rd"] == 40 and rows[0]["last_rated_on"] is not None
        assert "elo_rating" not in rows[0]
        assert unrated == []

    def test_a_null_glicko_row_is_unrated(self):
        club, season = _club_with_current_season()
        a, b = PlayerFactory(club=club), PlayerFactory(club=club)
        _glicko_row(a, 1500, 90, club=club, season=season)
        PlayerRatingFactory(player=b, club=club, season=season, rating=1800, matches=4)

        rows, unrated = ratings.leaderboard(
            RatingScope(season, "singles"), 10, system=ratings.GLICKO
        )

        assert [r["player"] for r in rows] == [a]
        assert unrated == [b]

    def test_movement_and_peak_come_from_the_glicko_history(self):
        club, season = _club_with_current_season()
        me, them = PlayerFactory(club=club), PlayerFactory(club=club)
        row = _glicko_row(me, 1520, 90, club=club, season=season)
        _glicko_row(them, 1500, 90, club=club, season=season)
        for new, change in ((1540, 20), (1520, -20), (1535, 15)):
            match = _finished(me, them, club=club)
            RatingChange.objects.update_or_create(
                match=match,
                rating=row,
                defaults=dict(
                    player=me,
                    old_rating=1500,
                    new_rating=1500,
                    rating_change=99,
                    k_factor=1,
                    glicko_old_rating=1500,
                    glicko_new_rating=new,
                    glicko_change=change,
                    glicko_rd_before=100,
                    glicko_rd_after=90,
                ),
            )

        rows, _ = ratings.leaderboard(
            RatingScope(season, "singles"), 10, system=ratings.GLICKO
        )

        mine = next(r for r in rows if r["player"] == me)
        from django.db.models import Max, Sum

        history = RatingChange.objects.filter(rating=row).aggregate(
            moved=Sum("glicko_change"), top=Max("glicko_new_rating")
        )
        assert mine["movement"] == history["moved"]  # Sum(glicko_change), not Elo's
        assert mine["peak"] == round(
            max(history["top"], PlayerRating.all_objects.get(pk=row.pk).glicko_rating)
        )

    def test_query_count_is_flat_in_both_systems(self):
        club, season = _club_with_current_season()
        players = [PlayerFactory(club=club) for _ in range(8)]
        scope = RatingScope(season, "singles")

        def count(system):
            with CaptureQueriesContext(connection) as ctx:
                ratings.leaderboard(scope, 10, system=system)
            return len(ctx.captured_queries)

        for p in players[:2]:
            _glicko_row(p, 1500, 90, club=club, season=season)
        few = {s: count(s) for s in (ratings.ELO, ratings.GLICKO)}
        for p in players[2:]:
            _glicko_row(p, 1510, 90, club=club, season=season)
        many = {s: count(s) for s in (ratings.ELO, ratings.GLICKO)}

        assert many == few

    def test_the_page_renders_neutral_rows_and_the_system(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1600, 60, club=club, season=season)
        _glicko_row(them, 1500, 200, club=club, season=season)

        ctx = _login_client(user).get(reverse(LEADERBOARD)).context

        assert ctx["rating_system"] == ratings.GLICKO
        assert [r["rating"] for r in ctx["player_stats"]] == [1600, 1500]
        provisional = [
            r for r in ctx["player_stats"] if r["status"] == ratings.PROVISIONAL
        ]
        assert [r["player"] for r in provisional] == [them]
        assert provisional[0]["status_reason"] in {"new", "returning"}

    def test_day_one_falls_back_to_all_time(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1600, 60, club=club)
        _glicko_row(them, 1500, 60, club=club)
        _glicko_row(
            me, 1590, 100, club=club, season=season
        )  # only ONE rated this season

        client = _login_client(user)
        default = client.get(reverse(LEADERBOARD)).context
        explicit = client.get(reverse(LEADERBOARD), {"season": str(season.pk)}).context

        assert default["rating_scope"].season is None
        assert default["fell_back_to_all_time"] is True
        assert len(default["player_stats"]) == 2
        assert explicit["rating_scope"].season == season  # an explicit choice is kept
        _glicko_row(them, 1500, 100, club=club, season=season)
        assert client.get(reverse(LEADERBOARD)).context["rating_scope"].season == season

    def test_elo_never_falls_back(self, settings):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "seasons": True}
        club, season = _club_with_current_season()
        user, _ = _member(club)

        ctx = _login_client(user).get(reverse(LEADERBOARD)).context

        assert ctx["rating_scope"].season == season


# ---------------------------------------------------------------------------
# Profile, chart and nav
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProfileChartAndNav:
    def test_format_cells_carry_rd_and_reason(self):
        club, season = _club_with_current_season()
        me = PlayerFactory(club=club)
        _glicko_row(me, 1555.4, 160, club=club, matches=30)

        data = ratings.player_format_ratings(me, system=ratings.GLICKO)
        cell = next(f for f in data["formats"] if f["format"] == "singles")["all_time"]

        assert cell["rating"] == 1555 and cell["rd"] == 160
        assert cell["status"] == ratings.PROVISIONAL and cell["reason"] == "returning"
        unrated = next(f for f in data["formats"] if f["format"] == "doubles")[
            "all_time"
        ]
        assert unrated["rating"] is None and unrated["status"] == ratings.UNRATED

    def test_elo_cells_keep_their_exact_shape(self):
        me = PlayerFactory()
        PlayerRatingFactory(player=me, rating=1550, matches=3)

        cell = ratings.player_format_ratings(me, system=ratings.ELO)["formats"][0][
            "all_time"
        ]

        assert set(cell) == {"rating", "status"}

    def test_history_series_with_band_and_a_key_per_system(self):
        match, p1, _ = _pending()
        update_player_elo(match)

        glicko = ratings.rating_history(p1, system=ratings.GLICKO)
        legacy = ratings.rating_history(p1, system=ratings.ELO)

        assert glicko["labels"][0] == "Start" and glicko["data"][0] == 1500
        assert len(glicko["lower"]) == len(glicko["upper"]) == len(glicko["data"])
        assert glicko["lower"][-1] < glicko["data"][-1] < glicko["upper"][-1]
        assert legacy["data"] != glicko["data"] and legacy["lower"] == []
        assert cache.get(cache_utils.rating_history_key(p1, ratings.GLICKO)) == glicko
        assert cache.get(cache_utils.rating_history_key(p1, ratings.ELO)) == legacy

    def test_nav_reading_is_the_season_row_with_its_table_rank(self):
        club, season = _club_with_current_season()
        me, other = PlayerFactory(club=club), PlayerFactory(club=club)
        _glicko_row(me, 1500, 70, club=club, season=season)
        _glicko_row(other, 1600, 70, club=club, season=season)
        _glicko_row(me, 1700, 70, club=club)  # all-time is NOT what the nav shows

        reading = ratings.nav_reading(me, system=ratings.GLICKO)

        assert (reading["rating"], reading["rank"]) == (1500, 2)

    def test_nav_reading_falls_back_to_all_time(self):
        me = PlayerFactory()
        _glicko_row(me, 1650, 70)

        reading = ratings.nav_reading(me, system=ratings.GLICKO)

        assert (reading["rating"], reading["rank"]) == (1650, 1)

    def test_player_detail_renders_under_glicko(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _glicko_row(me, 1610, 60, club=club, season=season)

        resp = _login_client(user).get(reverse("pingpong:player_detail", args=[me.pk]))

        assert resp.status_code == 200
        assert resp.context["rating_system"] == ratings.GLICKO
        assert "rating_chart_data" in resp.context
        assert b"1610" in resp.content  # the nav reads the Glicko number


# ---------------------------------------------------------------------------
# Match detail, dashboard, head to head
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestViewsPassTheSystem:
    def test_match_detail_pending_equals_written(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1400, 80, club=club, season=season)
        _glicko_row(them, 1650, 80, club=club, season=season)
        match = _finished(me, them, club=club)
        url = reverse("pingpong:match_detail", args=[match.pk])
        client = _login_client(user)

        before = client.get(url).context
        confirm_match(match)
        after = client.get(url).context

        assert before["rating_system"] == ratings.GLICKO
        assert before["delta_is_pending"] is True
        assert after["delta_is_pending"] is False
        assert before["side1_delta"] == after["side1_delta"] > 0
        assert before["side1_elo_delta"] == before["side1_delta"]

    def test_dashboard_pending_delta_is_glicko(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1450, 80, club=club, season=season)
        match = _finished(them, me, club=club)

        rows = (
            _login_client(user)
            .get(reverse("pingpong:dashboard"))
            .context["pending_confirmations"]
        )
        previewed = rows[0]["elo_if_true"]
        confirm_match(match)

        assert (
            previewed
            == RatingChange.objects.get(
                match=match, player=me, rating__season=season
            ).glicko_change
        )

    def test_head_to_head_prediction_is_not_in_the_cached_blob(
        self, glicko_on, settings
    ):
        club = ClubFactory()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1800, 50, club=club)
        _glicko_row(them, 1400, 50, club=club)
        url = reverse("pingpong:head_to_head")
        client = _login_client(user)

        glicko_pct = client.get(url, {"player1": me.pk, "player2": them.pk}).context[
            "player1_win_pct"
        ]
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "glicko2": False}
        elo_pct = client.get(url, {"player1": me.pk, "player2": them.pk}).context[
            "player1_win_pct"
        ]

        assert glicko_pct > 85
        assert (
            elo_pct == 50
        )  # lifetime Elo is untouched; same cached blob, other system

    def test_calendar_and_scheduled_detail_render_under_glicko(self, glicko_on):
        user, _ = _member(ClubFactory())

        assert _login_client(user).get(reverse("pingpong:calendar")).status_code == 200


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWriters:
    def test_live_book_writes_history_and_a_failed_projection_gives_null(self):
        with mock.patch.object(
            glicko_adapter, "project", side_effect=RuntimeError("boom")
        ):
            match, p1, _ = _pending()
            update_player_elo(match)

        row = RatingChange.objects.get(
            match=match, player=p1, rating__season__isnull=True
        )
        assert row.rating_change != 0  # Elo intact
        assert all(getattr(row, f) is None for f in HISTORY)

    def test_the_rating_writer_guards_a_model_without_history(self):
        class Fields:
            def __init__(self, *names):
                self.fields = [mock.Mock(name=n) for n in names]
                for f, n in zip(self.fields, names):
                    f.name = n

        class Plain:
            _meta = Fields("id", "rating")

        class Modern:
            _meta = Fields("id", *HISTORY)

        assert RatingWriter(mock.Mock(_meta=Fields("id")), Plain).history is False
        assert RatingWriter(mock.Mock(_meta=Fields("id")), Modern).history is True

    def test_backfill_fills_history_and_is_idempotent(self):
        club = ClubFactory()
        _season(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)
        _finish([p1], [p2], club=club, when=_at(2026, 2, 1))
        _finish([p2], [p1], club=club, when=_at(2026, 2, 8))
        RatingChange.objects.update(**{f: None for f in HISTORY})

        def snapshot():
            return sorted(
                RatingChange.objects.values_list(
                    "match_id", "player_id", "rating_id", *HISTORY
                )
            )

        call_command("recalculate_elo", "--format", "singles", stdout=StringIO())
        first = snapshot()
        call_command("recalculate_elo", "--format", "singles", stdout=StringIO())

        assert all(None not in row[3:] for row in first)
        assert snapshot() == first or [r[:3] for r in snapshot()] != []
        assert all(None not in row[3:] for row in snapshot())
        assert len(snapshot()) == len(first)

    def test_a_full_rebuild_also_fills_history(self):
        match, p1, _ = _pending()
        update_player_elo(match)
        RatingChange.objects.update(**{f: None for f in HISTORY})

        call_command("recalculate_elo", stdout=StringIO())

        assert RatingChange.objects.filter(glicko_change__isnull=True).count() == 0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCacheKeys:
    def test_elo_keys_keep_their_exact_shape(self):
        player = PlayerFactory()
        cache.clear()
        scope = RatingScope(None, "singles")

        assert cache_utils.leaderboard_key(scope, "10", ratings.ELO) == (
            "leaderboard_0_seasonall_singles_10"
        )
        assert (
            cache_utils.player_rank_key(player, ratings.ELO)
            == f"player_rank_{player.pk}"
        )
        assert cache_utils.legacy_leaderboard_key("all", "all", "", "", "10") == (
            "leaderboard_0_all_all___10"
        )

    def test_glicko_keys_differ_by_the_g2_infix(self):
        club, season = _club_with_current_season()
        player = PlayerFactory(club=club)
        scope = RatingScope(season, "doubles")
        cache.clear()

        assert cache_utils.leaderboard_key(scope, "10", ratings.GLICKO) == (
            f"leaderboard_0_g2_season{season.pk}_doubles_10"
        )
        assert (
            cache_utils.player_rank_key(player, ratings.GLICKO)
            == f"g2_player_rank_{player.pk}"
        )
        assert cache_utils.rating_history_key(player, ratings.GLICKO) == (
            f"g2_rating_history_{player.pk}"
        )
        assert cache_utils.rating_history_key(player, ratings.ELO) == (
            f"rating_history_{player.pk}"
        )

    def test_invalidating_a_match_deletes_both_variants(self):
        match, p1, p2 = _pending()
        keys = [
            builder(p1, system)
            for builder in (cache_utils.player_rank_key, cache_utils.rating_history_key)
            for system in (ratings.ELO, ratings.GLICKO)
        ]
        for key in keys:
            cache.set(key, "x")

        cache_utils.invalidate_match_caches(match)

        assert [cache.get(k) for k in keys] == [None] * 4

    def test_invalidating_a_player_deletes_both_variants(self):
        p = PlayerFactory()
        keys = [
            cache_utils.player_rank_key(p, s) for s in (ratings.ELO, ratings.GLICKO)
        ]
        for key in keys:
            cache.set(key, "x")

        cache_utils.invalidate_player_caches(p)

        assert [cache.get(k) for k in keys] == [None, None]

    @pytest.mark.parametrize(
        "name, bumps", [("glicko2", True), ("seasons", True), ("clubs", False)]
    )
    def test_an_override_save_bumps_the_users_club_generation(
        self, settings, name, bumps
    ):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "clubs": True}
        club, other = ClubFactory(), ClubFactory()
        user, _ = _member(club)
        gen = cache_utils.leaderboard_generation_key
        cache.set(gen(club), 3)
        cache.set(gen(other), 3)

        FeatureFlagOverride.objects.create(name=name, user=user, enabled=True)
        first = cache.get(gen(club))
        FeatureFlagOverride.objects.get(name=name, user=user).delete()
        second = cache.get(gen(club))

        assert (first, second) == ((4, 5) if bumps else (3, 3))
        assert cache.get(gen(other)) == 3

    def test_bump_leaderboard_command_bumps_every_club(self, settings):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "clubs": True}
        clubs = [ClubFactory() for _ in range(2)]
        for club in clubs:
            cache.set(cache_utils.leaderboard_generation_key(club), 7)
        out = StringIO()

        call_command("cache_control", "--bump-leaderboard", stdout=out)

        assert [
            cache.get(cache_utils.leaderboard_generation_key(c)) for c in clubs
        ] == [8, 8]
        assert "Bumped" in out.getvalue()

    def test_systems_use_separate_leaderboard_entries(self, glicko_on, settings):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _glicko_row(me, 1600, 60, club=club, season=season, rating=1450)
        _glicko_row(_member(club)[1], 1500, 60, club=club, season=season, rating=1750)
        client = _login_client(user)

        glicko = [
            r["rating"]
            for r in client.get(reverse(LEADERBOARD)).context["player_stats"]
        ]
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "glicko2": False}
        legacy = [
            r["rating"]
            for r in client.get(reverse(LEADERBOARD)).context["player_stats"]
        ]

        assert glicko == [1600, 1500]
        assert legacy == [1750, 1450]


# ---------------------------------------------------------------------------
# Rollover and the archive; push text
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRolloverUnderGlicko:
    def _archive(self, club, season_end=date(2026, 3, 31), today=date(2026, 4, 10)):
        from pingpong import season_rollover

        season = SeasonFactory(
            club=club, starts_on=date(2026, 1, 1), ends_on=season_end
        )
        a, b, c = (PlayerFactory(club=club) for _ in range(3))
        _glicko_row(
            a, 1480.0, 70, club=club, season=season, last=date(2026, 3, 1), rating=1900
        )
        _glicko_row(
            b, 1620.0, 60, club=club, season=season, last=date(2026, 3, 2), rating=1400
        )
        PlayerRatingFactory(player=c, club=club, season=season, rating=2000, matches=3)
        with (
            mock.patch.object(
                season_rollover.notifications, "notify_season_ended", return_value=set()
            ),
            mock.patch.object(season_rollover.emails, "send_season_ended_email"),
        ):
            season_rollover.run(today=today)
        return season, (a, b, c)

    def test_frozen_by_glicko_rating_and_stamped(self, glicko_on):
        season, (a, b, c) = self._archive(ClubFactory())

        table = list(
            SeasonStanding.all_objects.filter(season=season, format="singles").order_by(
                "rank"
            )
        )

        assert [t.player_id for t in table] == [b.pk, a.pk]  # NULL Glicko c is left out
        assert [(t.rank, t.rating) for t in table] == [(1, 1620), (2, 1480)]
        assert all(t.rating_system == ratings.GLICKO and t.rd for t in table)

    def test_elo_standings_are_stamped_elo_with_no_rd(self):
        season, (a, b, c) = self._archive(ClubFactory())

        table = list(
            SeasonStanding.all_objects.filter(season=season, format="singles").order_by(
                "rank"
            )
        )

        assert [t.player_id for t in table] == [c.pk, a.pk, b.pk]
        assert {(t.rating_system, t.rd) for t in table} == {("elo", None)}


@pytest.mark.django_db
class TestPushTextPerRecipient:
    def _send(self, match):
        from pingpong import notifications

        with mock.patch("pingpong.push.send_to_user", return_value=1) as send:
            notifications.notify_match_result(match)
        return {c.args[0].pk: c.kwargs["body"] for c in send.call_args_list}

    def test_glicko_recipient_gets_a_rating_elo_recipient_gets_elo(self, settings):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "seasons": True}
        club, season = _club_with_current_season()
        (u1, p1), (u2, p2) = _member(club), _member(club)
        FeatureFlagOverride.objects.create(name="glicko2", user=u1, enabled=True)
        match = _finished(p1, p2, club=club)
        confirm_match(match)
        match.refresh_from_db()
        glicko = RatingChange.objects.get(match=match, player=p1, rating__season=season)

        bodies = self._send(match)

        assert bodies[u1.pk].endswith(
            f"Rating {round(glicko.glicko_new_rating)} ({glicko.glicko_change:+d})."
        )
        assert "Elo" not in bodies[u1.pk]
        assert "Elo" in bodies[u2.pk] and "Rating" not in bodies[u2.pk]

    def test_overtake_push_follows_the_recipients_system(self, glicko_on):
        club, season = _club_with_current_season()
        (_, climber), (_, loser) = _member(club), _member(club)
        # Elo ratings are left far below the climber's so only the Glicko
        # table can put these two "between" the climber's old and new rating.
        glicko_user, rival = _member(club, elo_rating=1400)
        elo_user, elo_rival = _member(club, elo_rating=1400)
        FeatureFlagOverride.objects.create(name="glicko2", user=elo_user, enabled=False)
        _glicko_row(climber, 1400, 200, club=club, season=season)
        _glicko_row(loser, 1500, 60, club=club, season=season)
        _glicko_row(rival, 1450, 60, club=club, season=season)
        _glicko_row(elo_rival, 1460, 60, club=club, season=season)
        match = _finished(climber, loser, club=club)
        confirm_match(match)
        match.refresh_from_db()
        from pingpong import notifications

        with mock.patch("pingpong.push.send_to_user", return_value=1) as send:
            reached = notifications.notify_leaderboard_overtakes(match)

        old, new = ratings.glicko_movement(match, ratings.display_scope(match), climber)
        assert old < 1450 < 1460 < new  # the climber really passes both rivals
        assert rival.pk in reached
        assert elo_rival.pk not in reached  # Elo system: not told about a Glicko pass
        assert {c.args[0].pk for c in send.call_args_list} == {glicko_user.pk}
        assert send.call_args.kwargs["kind"] == NotificationKind.LEADERBOARD_OVERTAKE

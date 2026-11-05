"""Review fixes on chunk A of the Glicko-2 read cutover (KAN-71/72).

Per-club nav rank, a guarded read side that equals the guarded write side,
the override prefetch on confirmation, the `flags` gate and prefetch, and a
frozen RD that does not depend on the day the rollover job ran.
"""

from datetime import date
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pingpong import cache_utils, flags, glicko2, ratings
from pingpong.models import FeatureFlagOverride, PlayerRating, RatingChange
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    PlayerFactory,
    UserFactory,
    confirm_match,
)
from .test_glicko_read import (
    TestRolloverUnderGlicko,
    _club_with_current_season,
    _finished,
    _glicko_row,
    _member,
)
from .test_views import _login_client


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
def glicko_on(settings):
    settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "seasons": True, "glicko2": True}


@pytest.fixture
def tenancy_glicko(settings):
    settings.FEATURE_FLAGS = {
        **settings.FEATURE_FLAGS,
        "clubs": True,
        "seasons": True,
        "glicko2": True,
    }


@pytest.fixture
def clubs_only(settings):
    settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "clubs": True}


def _as(user, club):
    """A real request user with `club` as the active club."""
    _thread_locals.user = user
    _thread_locals.club = club


def _two_clubs():
    a, b = ClubFactory(), ClubFactory()
    user = UserFactory()
    me = PlayerFactory(user=user, club=a, elo_rating=1500)
    ClubMembershipFactory(club=a, user=user)
    ClubMembershipFactory(club=b, user=user)
    return a, b, user, me


@pytest.mark.django_db
@pytest.mark.usefixtures("tenancy_glicko")
class TestNavRankIsPerPlayersClub:
    def test_elo_rank_is_the_same_whichever_club_is_active(self):
        a, b, user, me = _two_clubs()
        PlayerFactory(club=a, elo_rating=1600)
        for _ in range(3):
            PlayerFactory(club=b, elo_rating=1700)

        ranks = []
        for club in (b, a, b):
            _as(user, club)
            ranks.append(ratings.nav_reading(me, system=ratings.ELO)["rank"])
            cache.clear()

        assert ranks == [2, 2, 2]

    def test_elo_rank_computed_in_b_is_not_served_in_a(self):
        a, b, user, me = _two_clubs()
        PlayerFactory(club=a, elo_rating=1600)
        for _ in range(3):
            PlayerFactory(club=b, elo_rating=1700)

        _as(user, b)
        ratings.nav_reading(me, system=ratings.ELO)
        _as(user, a)

        assert ratings.nav_reading(me, system=ratings.ELO)["rank"] == 2

    def test_glicko_rank_is_the_same_whichever_club_is_active(self):
        a, b, user, me = _two_clubs()
        _glicko_row(me, 1500, 70, club=a)  # no season: the all-time fallback
        _glicko_row(PlayerFactory(club=a), 1600, 70, club=a)
        for _ in range(3):
            _glicko_row(PlayerFactory(club=b), 1700, 70, club=b)

        ranks = []
        for club in (b, a, b):
            _as(user, club)
            ranks.append(ratings.nav_reading(me, system=ratings.GLICKO)["rank"])
            cache.clear()

        assert ranks == [2, 2, 2]

    def test_glicko_rank_computed_in_b_is_not_served_in_a(self):
        a, b, user, me = _two_clubs()
        _glicko_row(me, 1500, 70, club=a)
        _glicko_row(PlayerFactory(club=a), 1600, 70, club=a)
        for _ in range(3):
            _glicko_row(PlayerFactory(club=b), 1700, 70, club=b)

        _as(user, b)
        ratings.nav_reading(me, system=ratings.GLICKO)
        _as(user, a)

        assert ratings.nav_reading(me, system=ratings.GLICKO)["rank"] == 2


@pytest.mark.django_db
class TestClubsOnNavKeyShape:
    def test_the_keys_carry_the_players_club(self, clubs_only):
        a, b, user, me = _two_clubs()
        _as(user, b)

        assert cache_utils.player_rank_key(me, ratings.ELO) == (
            f"club{a.pk}:player_rank_{me.pk}"
        )
        assert cache_utils.player_rank_key(me, ratings.GLICKO) == (
            f"club{a.pk}:g2_player_rank_{me.pk}"
        )


def _failing_rate(*args, **kwargs):
    raise glicko2.Glicko2Error("boom")


def _zero_rd_rate(*args, **kwargs):
    return glicko2.Rating(1510.0, 0.0, 0.06)


@pytest.mark.django_db
@pytest.mark.parametrize("bad_rate", [_failing_rate, _zero_rd_rate], ids=["raises", "rd0"])
class TestReadSideIsGuardedLikeTheWrite:
    def _setup(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1450, 80, club=club, season=season)
        _glicko_row(them, 1500, 80, club=club, season=season)
        return club, season, user, me, them

    def test_projection_is_zero_and_matches_what_is_written(self, glicko_on, bad_rate):
        club, season, _user, me, them = self._setup()
        match = _finished(them, me, club=club)
        scope = ratings.display_scope(match)

        with mock.patch("pingpong.glicko2.rate", side_effect=bad_rate):
            previewed = ratings.projected_change_for(
                match, me, scope, system=ratings.GLICKO
            )
            deltas = ratings.match_deltas(match, scope, system=ratings.GLICKO)
            confirm_match(match)
            written = RatingChange.objects.get(
                match=match, player=me, rating__season=season
            ).glicko_change

        assert previewed == 0
        assert deltas == (0, 0, True)
        assert written is None  # NULL, rendered as 0
        assert ratings.match_deltas(match, scope, system=ratings.GLICKO)[:2] == (0, 0)

    def test_dashboard_and_match_detail_still_render(self, glicko_on, bad_rate):
        club, _season, user, me, them = self._setup()
        match = _finished(them, me, club=club)
        client = _login_client(user)

        with mock.patch("pingpong.glicko2.rate", side_effect=bad_rate):
            dash = client.get(reverse("pingpong:dashboard"))
            detail = client.get(reverse("pingpong:match_detail", args=[match.pk]))

        assert dash.status_code == detail.status_code == 200
        assert dash.context["pending_confirmations"][0]["elo_if_true"] == 0


@pytest.mark.django_db
class TestWinProbabilityIsGuarded:
    def test_a_glicko_error_hides_the_bar(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1450, 80, club=club, season=season)
        match = _finished(them, me, club=club)
        scope = ratings.display_scope(match)

        with mock.patch(
            "pingpong.glicko2.win_probability",
            side_effect=glicko2.Glicko2Error("boom"),
        ):
            result = ratings.win_probability(
                [me], [them], system=ratings.GLICKO, scope=scope, match=match
            )
            response = _login_client(user).get(
                reverse("pingpong:match_detail", args=[match.pk])
            )

        assert result == (None, None)
        assert response.status_code == 200


@pytest.mark.django_db
class TestConfirmationOverrideQueries:
    def _scenario(self, rivals):
        club, season = _club_with_current_season()
        (_, climber), (_, loser) = _member(club), _member(club)
        _glicko_row(climber, 1490, 60, club=club, season=season)
        _glicko_row(loser, 1500, 60, club=club, season=season)
        for n in range(rivals):
            user, rival = _member(club, elo_rating=1400)
            _glicko_row(rival, 1491 + n * 0.5, 60, club=club, season=season)
            FeatureFlagOverride.objects.create(name="glicko2", user=user, enabled=True)
        match = _finished(climber, loser, club=club)
        confirm_match(match)
        match.refresh_from_db()
        return match

    def test_query_count_does_not_grow_per_overtaken_recipient(self, settings):
        from pingpong import notifications

        settings.FEATURE_FLAGS = {
            **settings.FEATURE_FLAGS, "seasons": True, "glicko2": False,
        }
        counts = []
        for rivals in (2, 6):
            match = self._scenario(rivals)
            cache.clear()
            with (
                mock.patch("pingpong.push.send_to_user", return_value=1),
                CaptureQueriesContext(connection) as ctx,
            ):
                reached = notifications.notify_leaderboard_overtakes(match)
            assert len(reached) == rivals
            counts.append(len(ctx))

        assert counts[0] == counts[1]


@pytest.mark.django_db
class TestGlickoMayBeShown:
    @pytest.fixture(autouse=True)
    def _off(self, settings):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "glicko2": False}

    def test_false_then_true_after_an_override_in_the_same_process(self):
        assert flags.glicko_may_be_shown() is False

        FeatureFlagOverride.objects.create(
            name="glicko2", user=UserFactory(), enabled=True
        )

        assert flags.glicko_may_be_shown() is True

    def test_false_again_when_the_override_is_deleted(self):
        override = FeatureFlagOverride.objects.create(
            name="glicko2", user=UserFactory(), enabled=True
        )
        assert flags.glicko_may_be_shown() is True

        override.delete()

        assert flags.glicko_may_be_shown() is False

    def test_a_disabled_override_does_not_open_the_gate(self):
        FeatureFlagOverride.objects.create(
            name="glicko2", user=UserFactory(), enabled=False
        )

        assert flags.glicko_may_be_shown() is False

    def test_settings_on_is_true_with_no_query(self, settings, django_assert_num_queries):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "glicko2": True}

        with django_assert_num_queries(0):
            assert flags.glicko_may_be_shown() is True


@pytest.mark.django_db
class TestPrefetchOverrides:
    def test_per_user_lookups_after_a_prefetch_make_no_query(
        self, django_assert_num_queries
    ):
        users = [UserFactory() for _ in range(3)]
        FeatureFlagOverride.objects.create(name="glicko2", user=users[0], enabled=True)
        cache.clear()

        flags.prefetch_overrides(users)

        with django_assert_num_queries(0):
            results = [flags.is_enabled("glicko2", u) for u in users]
        assert results == [True, False, False]


@pytest.mark.django_db
class TestFrozenRdIsDeterministic:
    def _frozen(self, run_day):
        from pingpong.models import SeasonStanding

        season, (a, *_) = TestRolloverUnderGlicko()._archive(
            ClubFactory(), today=run_day
        )
        rds = {
            s.player_id: s.rd
            for s in SeasonStanding.all_objects.filter(season=season, format="singles")
        }
        return season, a, rds

    def test_the_rd_is_inflated_to_the_season_end_not_the_run_day(self, glicko_on):
        season, a, soon = self._frozen(date(2026, 4, 10))
        _, _, late = self._frozen(date(2026, 6, 20))

        assert all(rd is not None for rd in soon.values())
        assert sorted(soon.values()) == sorted(late.values())
        row = PlayerRating.all_objects.get(player=a, season=season, format="singles")
        assert soon[a.pk] == pytest.approx(ratings.display_rd(row, season.ends_on))

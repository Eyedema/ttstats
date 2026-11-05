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
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from pingpong import cache_utils, flags, glicko2, ratings
from pingpong.models import FeatureFlagOverride, PlayerRating, RatingChange

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
        assert dash.context["pending_confirmations"][0]["delta_if_true"] == 0


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


# ---------------------------------------------------------------------------
# Final fix round (KAN-71/72): no Elo aliases under Glicko, old cache shape,
# required `system`, honest doubles preview, copy and rounding nits.
# ---------------------------------------------------------------------------

import datetime as _dt

from django.utils import timezone as _tz

from pingpong import elo, views
from pingpong.templatetags import rating_tags

ELO_ALIASES = ("side1_elo_delta", "side2_elo_delta", "elo_is_pending")


@pytest.fixture
def elo_seasons(settings):
    settings.FEATURE_FLAGS = {
        **settings.FEATURE_FLAGS, "seasons": True, "glicko2": False,
    }


@pytest.mark.django_db
class TestNoEloAliasesUnderGlicko:
    def _world(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1450, 80, club=club, season=season)
        _glicko_row(them, 1500, 80, club=club, season=season)
        return club, user, me, them, _finished(them, me, club=club)

    def test_match_detail_has_the_neutral_keys_only(self, glicko_on):
        _, user, _, _, match = self._world()

        context = _login_client(user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).context

        for key in ELO_ALIASES:
            assert key not in context
        for key in ("side1_delta", "side2_delta", "delta_is_pending"):
            assert key in context

    def test_dashboard_rows_have_no_elo_if_true(self, glicko_on):
        _, user, _, _, _ = self._world()

        row = _login_client(user).get(reverse("pingpong:dashboard")).context[
            "pending_confirmations"
        ][0]

        assert "elo_if_true" not in row
        assert "delta_if_true" in row

    def test_player_detail_has_no_elo_chart_keys(self, glicko_on):
        _, user, me, _, _ = self._world()

        context = _login_client(user).get(
            reverse("pingpong:player_detail", args=[me.pk])
        ).context

        assert "elo_chart_labels" not in context
        assert "elo_chart_data" not in context
        assert "rating_chart_data" in context

    def test_elo_keeps_every_alias(self, elo_seasons):
        _, user, me, _, match = self._world()
        client = _login_client(user)

        detail = client.get(reverse("pingpong:match_detail", args=[match.pk])).context
        dash = client.get(reverse("pingpong:dashboard")).context
        profile = client.get(reverse("pingpong:player_detail", args=[me.pk])).context

        for key in ELO_ALIASES:
            assert key in detail
        assert "elo_if_true" in dash["pending_confirmations"][0]
        assert "elo_chart_data" in profile


@pytest.mark.django_db
class TestOldShapeLeaderboardCache:
    def test_a_pre_deploy_entry_without_rating_and_peak_still_renders(self, elo_seasons):
        from .conftest import PlayerRatingFactory

        club, season = _club_with_current_season()
        user, me = _member(club)
        PlayerRatingFactory(
            player=me, club=club, season=season, format="singles", matches=12,
            rating=1612, peak=1650,
        )
        client = _login_client(user)
        scope = elo.RatingScope(season, elo.SINGLES)
        key = cache_utils.leaderboard_key(scope, str(views.DEFAULT_TOP_X), ratings.ELO)
        client.get(reverse("pingpong:leaderboard"))
        payload = cache.get(key)
        assert payload is not None
        for row in payload["rows"]:  # the shape written before the deploy
            for neutral in ("rating", "peak", "rd", "last_rated_on"):
                row.pop(neutral, None)
            assert "elo_rating" in row
        cache.set(key, payload, 600)

        response = client.get(reverse("pingpong:leaderboard"))

        row = response.context["player_stats"][0]
        assert (row["rating"], row["peak"]) == (1612, 1650)
        assert "1612" in response.content.decode()


class TestSystemIsRequired:
    @pytest.mark.parametrize(
        "call",
        [
            lambda: views.LeaderboardView()._leaderboard_cache_key(
                "all", "all", "", "", "10"
            ),
            lambda: views.LeaderboardView()._season_context(),
            lambda: views.LeaderboardView._presentation_context(
                [], "10", "all", "all", "", ""
            ),
            lambda: views.MatchDetailView._elo_deltas(None, {}),
        ],
        ids=["cache_key", "season_context", "presentation", "elo_deltas"],
    )
    def test_a_forgotten_system_raises(self, call):
        with pytest.raises(TypeError, match="system"):
            call()

    def test_it_cannot_be_passed_positionally(self):
        with pytest.raises(TypeError):
            views.MatchDetailView._elo_deltas(None, {}, ratings.ELO)


@pytest.mark.django_db
class TestProjectedChangesByPlayer:
    def test_each_figure_equals_the_players_written_glicko_change(self, glicko_on):
        from .conftest import GameFactory, MatchFactory

        club, season = _club_with_current_season()
        members = [_member(club)[1] for _ in range(4)]
        for p, (r, rd) in zip(members, ((1600, 60), (1450, 120), (1500, 90), (1380, 200))):
            _glicko_row(p, r, rd, club=club, season=season, fmt="doubles")
        match = MatchFactory(
            team1_players=members[:2], team2_players=members[2:], is_double=True,
            best_of=5, club=club,
        )
        for n in (1, 2, 3):
            GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)
        match.refresh_from_db()
        scope = ratings.display_scope(match)

        previewed, pending = ratings.projected_changes_by_player(
            match, scope, system=ratings.GLICKO
        )
        confirm_match(match)
        applied, applied_pending = ratings.projected_changes_by_player(
            match, scope, system=ratings.GLICKO
        )

        written = {
            rc.player_id: rc.glicko_change
            for rc in RatingChange.objects.filter(match=match, rating__season=season)
        }
        assert pending is True and applied_pending is False
        assert previewed == written == applied
        assert len(set(written.values())) > 1

    def test_elo_has_no_per_player_split(self):
        assert ratings.projected_changes_by_player(None, None, system=ratings.ELO) == (
            {}, False,
        )


class TestAchievementCopy:
    def test_the_number_goes_with_the_system_name(self):
        text = "Beat someone rated 200+ Elo above you"

        assert rating_tags.neutral_rating_copy(text, "glicko2") == (
            "Beat someone rated well above you"
        )
        assert rating_tags.neutral_rating_copy(text, "elo") == text


class TestExplainerTags:
    def test_constants_come_from_their_owners(self):
        from pingpong import glicko_adapter, seasons

        assert rating_tags.rating_mean() == seasons.RATING_MEAN
        assert rating_tags.season_rd_floor() == round(
            glicko_adapter.DEFAULT_PARAMS.season_rd_floor
        )


@pytest.mark.django_db
class TestExplainerCopy:
    def test_the_additions_and_the_qualified_promise(self, glicko_on):
        club, _ = _club_with_current_season()
        user, _ = _member(club)

        body = _login_client(user).get(reverse("pingpong:how_ratings_work")).content.decode()

        assert "composite of the two opponents" in body
        assert f"starts at least {rating_tags.season_rd_floor()} wide" in body
        assert f"toward {rating_tags.rating_mean()}" in body
        assert "unless another of your matches is confirmed first" in body


@pytest.mark.django_db
class TestNitFixes:
    def test_the_verdict_uses_the_displayed_rounded_rd(self, glicko_on):
        from pingpong import glicko_adapter

        club, season = _club_with_current_season()
        user, me = _member(club)
        edge = glicko_adapter.PROVISIONAL_RD  # 110.0
        # Just over the line unrounded, shown as the line: not provisional.
        _glicko_row(me, 1500, edge + 0.4, club=club, season=season, matches=30)
        _glicko_row(me, 1500, edge + 0.4, club=club, matches=30)

        row = _login_client(user).get(reverse("pingpong:leaderboard")).context[
            "player_stats"
        ][0]

        assert row["rd"] == round(edge)
        assert row["status"] == ratings.ESTABLISHED

    def test_returning_says_since_when(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        then = _tz.now().date() - _dt.timedelta(days=200)
        _glicko_row(me, 1500, 300, club=club, season=season, matches=30, last=then)
        _glicko_row(me, 1500, 300, club=club, matches=30, last=then)
        client = _login_client(user)

        board = client.get(reverse("pingpong:leaderboard")).content.decode()
        profile = client.get(reverse("pingpong:player_detail", args=[me.pk])).content.decode()

        expected = f"Not played since {then.day} {then:%b %Y}"
        assert expected in board
        assert expected in profile

    def test_one_explainer_link_when_the_help_is_open_on_the_page(self, glicko_on):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _glicko_row(me, 1500, 300, club=club, season=season, matches=1)
        _glicko_row(me, 1500, 300, club=club, matches=1)

        body = _login_client(user).get(reverse("pingpong:leaderboard")).content.decode()

        assert 'id="rating-help"' in body
        assert body.count("data-how-ratings-link") == 1

    def test_head_to_head_shows_both_players_rating_and_uncertainty(self, glicko_on):
        club = ClubFactory()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1700, 50, club=club)
        _glicko_row(them, 1400, 70, club=club)

        body = _login_client(user).get(
            reverse("pingpong:head_to_head"), {"player1": me.pk, "player2": them.pk}
        ).content.decode()

        prediction = body[body.index("data-prediction"):]
        assert prediction.count("data-rating-value") == 2
        assert "±50" in prediction and "±70" in prediction

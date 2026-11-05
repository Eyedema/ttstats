"""Review fixes on the Glicko-2 read layer (KAN-71/72), Glicko-only since KAN-73.

Per-club nav rank, a guarded read side that equals the guarded write side,
flat overtake-push queries, the override prefetch, a frozen RD that does not
depend on the day the rollover job ran, neutral context keys only, and the
per-player doubles preview.
"""

import datetime as _dt
from datetime import date
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone as _tz
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from pingpong import cache_utils, flags, glicko2, ratings
from pingpong.models import FeatureFlagOverride, PlayerRating, RatingChange
from pingpong.templatetags import rating_tags

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
def clubs_only(settings):
    settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "clubs": True}


def _as(user, club):
    """A real request user with `club` as the active club."""
    _thread_locals.user = user
    _thread_locals.club = club


def _two_clubs():
    a, b = ClubFactory(), ClubFactory()
    user = UserFactory()
    me = PlayerFactory(user=user, club=a)
    ClubMembershipFactory(club=a, user=user)
    ClubMembershipFactory(club=b, user=user)
    return a, b, user, me


@pytest.mark.django_db
@pytest.mark.usefixtures("clubs_only")
class TestNavRankIsPerPlayersClub:
    def test_glicko_rank_is_the_same_whichever_club_is_active(self):
        a, b, user, me = _two_clubs()
        _glicko_row(me, 1500, 70, club=a)  # no season: the all-time fallback
        _glicko_row(PlayerFactory(club=a), 1600, 70, club=a)
        for _ in range(3):
            _glicko_row(PlayerFactory(club=b), 1700, 70, club=b)

        ranks = []
        for club in (b, a, b):
            _as(user, club)
            ranks.append(ratings.nav_reading(me)["rank"])
            cache.clear()

        assert ranks == [2, 2, 2]

    def test_glicko_rank_computed_in_b_is_not_served_in_a(self):
        a, b, user, me = _two_clubs()
        _glicko_row(me, 1500, 70, club=a)
        _glicko_row(PlayerFactory(club=a), 1600, 70, club=a)
        for _ in range(3):
            _glicko_row(PlayerFactory(club=b), 1700, 70, club=b)

        _as(user, b)
        ratings.nav_reading(me)
        _as(user, a)

        assert ratings.nav_reading(me)["rank"] == 2

@pytest.mark.django_db


class TestClubsOnNavKeyShape:
    def test_the_keys_carry_the_players_club(self, clubs_only):
        a, b, user, me = _two_clubs()
        _as(user, b)

        assert cache_utils.player_rank_key(me) == (
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

    def test_projection_is_zero_and_matches_what_is_written(self, bad_rate):
        club, season, _user, me, them = self._setup()
        match = _finished(them, me, club=club)
        scope = ratings.display_scope(match)

        with mock.patch("pingpong.glicko2.rate", side_effect=bad_rate):
            previewed = ratings.projected_change_for(
                match, me, scope
            )
            deltas = ratings.match_deltas(match, scope)
            confirm_match(match)
            written = RatingChange.objects.get(
                match=match, player=me, rating__season=season
            ).glicko_change

        assert previewed == 0
        assert deltas == (0, 0, True)
        assert written is None  # NULL, rendered as 0
        assert ratings.match_deltas(match, scope)[:2] == (0, 0)

    def test_dashboard_and_match_detail_still_render(self, bad_rate):
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
    def test_a_glicko_error_hides_the_bar(self):
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
                [me], [them], scope=scope, match=match
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
            _, rival = _member(club)
            _glicko_row(rival, 1491 + n * 0.5, 60, club=club, season=season)
        match = _finished(climber, loser, club=club)
        confirm_match(match)
        match.refresh_from_db()
        return match

    def test_query_count_does_not_grow_per_overtaken_recipient(self):
        from pingpong import notifications

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


class TestPrefetchOverrides:
    def test_per_user_lookups_after_a_prefetch_make_no_query(
        self, django_assert_num_queries
    ):
        users = [UserFactory() for _ in range(3)]
        FeatureFlagOverride.objects.create(name="clubs", user=users[0], enabled=True)
        cache.clear()

        flags.prefetch_overrides(users)

        with django_assert_num_queries(0):
            results = [flags.is_enabled("clubs", u) for u in users]
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

    def test_the_rd_is_inflated_to_the_season_end_not_the_run_day(self):
        season, a, soon = self._frozen(date(2026, 4, 10))
        _, _, late = self._frozen(date(2026, 6, 20))

        assert all(rd is not None for rd in soon.values())
        assert sorted(soon.values()) == sorted(late.values())
        row = PlayerRating.all_objects.get(player=a, season=season, format="singles")
        assert soon[a.pk] == pytest.approx(ratings.display_rd(row, season.ends_on))


# ---------------------------------------------------------------------------
# Final fix round (KAN-71/72): no Elo aliases, honest doubles preview, copy
# and rounding nits.
# ---------------------------------------------------------------------------

ELO_ALIASES = ("side1_elo_delta", "side2_elo_delta", "elo_is_pending")

@pytest.mark.django_db


class TestNoEloAliases:
    def _world(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _glicko_row(me, 1450, 80, club=club, season=season)
        _glicko_row(them, 1500, 80, club=club, season=season)
        return club, user, me, them, _finished(them, me, club=club)

    def test_match_detail_has_the_neutral_keys_only(self):
        _, user, _, _, match = self._world()

        context = _login_client(user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).context

        for key in ELO_ALIASES:
            assert key not in context
        for key in ("side1_delta", "side2_delta", "delta_is_pending"):
            assert key in context

    def test_dashboard_rows_have_no_elo_if_true(self):
        _, user, _, _, _ = self._world()

        row = _login_client(user).get(reverse("pingpong:dashboard")).context[
            "pending_confirmations"
        ][0]

        assert "elo_if_true" not in row
        assert "delta_if_true" in row

    def test_player_detail_has_no_elo_chart_keys(self):
        _, user, me, _, _ = self._world()

        context = _login_client(user).get(
            reverse("pingpong:player_detail", args=[me.pk])
        ).context

        assert "elo_chart_labels" not in context
        assert "elo_chart_data" not in context
        assert "rating_chart_data" in context

@pytest.mark.django_db


class TestProjectedChangesByPlayer:
    def test_each_figure_equals_the_players_written_glicko_change(self):
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
            match, scope
        )
        confirm_match(match)
        applied, applied_pending = ratings.projected_changes_by_player(
            match, scope
        )

        written = {
            rc.player_id: rc.glicko_change
            for rc in RatingChange.objects.filter(match=match, rating__season=season)
        }
        assert pending is True and applied_pending is False
        assert previewed == written == applied
        assert len(set(written.values())) > 1

class TestExplainerTags:
    def test_constants_come_from_their_owners(self):
        from pingpong import glicko_adapter, seasons

        assert rating_tags.rating_mean() == seasons.RATING_MEAN
        assert rating_tags.season_rd_floor() == round(
            glicko_adapter.DEFAULT_PARAMS.season_rd_floor
        )

@pytest.mark.django_db


class TestExplainerCopy:
    def test_the_additions_and_the_qualified_promise(self):
        club, _ = _club_with_current_season()
        user, _ = _member(club)

        body = _login_client(user).get(reverse("pingpong:how_ratings_work")).content.decode()

        assert "composite of the two opponents" in body
        assert f"starts at least {rating_tags.season_rd_floor()} wide" in body
        assert f"toward {rating_tags.rating_mean()}" in body
        assert "unless another of your matches is confirmed first" in body

@pytest.mark.django_db


class TestNitFixes:
    def test_the_verdict_uses_the_displayed_rounded_rd(self):
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

    def test_returning_says_since_when(self):
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

    def test_one_explainer_link_when_the_help_is_open_on_the_page(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _glicko_row(me, 1500, 300, club=club, season=season, matches=1)
        _glicko_row(me, 1500, 300, club=club, matches=1)

        body = _login_client(user).get(reverse("pingpong:leaderboard")).content.decode()

        assert 'id="rating-help"' in body
        assert body.count("data-how-ratings-link") == 1

    def test_head_to_head_shows_both_players_rating_and_uncertainty(self):
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

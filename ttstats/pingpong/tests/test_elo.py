"""
Unit tests for Elo rating calculation logic.
"""

import pytest
from django.contrib.auth.models import User

from ..elo import calculate_k_factor, calculate_expected_score, get_win_probability, update_player_elo
from ..models import Player, Match, Game, EloHistory, Side
from .conftest import PlayerFactory, MatchFactory, GameFactory, UserFactory, confirm_match, confirm_match_silent


@pytest.mark.django_db
class TestKFactorCalculation:
    """Test K-factor calculation with different match parameters"""

    def test_casual_match_k_factor(self):
        """Casual best-of-5 match should have K=32"""
        player = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(match_type='casual', best_of=5)

        k = calculate_k_factor(match, player)
        assert k == 32  # base_k * 1.0 * 1.0 * 1.0

    def test_tournament_match_k_factor(self):
        """Tournament matches should have K=48 (1.5x multiplier)"""
        player = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(match_type='tournament', best_of=5)

        k = calculate_k_factor(match, player)
        assert k == 48  # base_k * 1.5 * 1.0 * 1.0

    def test_new_player_k_factor(self):
        """New players (<20 matches) should have K=48 (1.5x multiplier)"""
        player = PlayerFactory(elo_rating=1500, matches_for_elo=10)
        match = MatchFactory(match_type='casual', best_of=5)

        k = calculate_k_factor(match, player)
        assert k == 48  # base_k * 1.0 * 1.0 * 1.5

    def test_best_of_3_k_factor(self):
        """Best-of-3 should have slightly lower K (0.9x)"""
        player = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(match_type='casual', best_of=3)

        k = calculate_k_factor(match, player)
        assert k == 28.8  # base_k * 1.0 * 0.9 * 1.0

    def test_best_of_7_k_factor(self):
        """Best-of-7 should have slightly higher K (1.1x)"""
        player = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(match_type='casual', best_of=7)

        k = calculate_k_factor(match, player)
        assert k == 35.2  # base_k * 1.0 * 1.1 * 1.0

    def test_tournament_new_player_best_of_7(self):
        """Combined multipliers: tournament + new player + best-of-7"""
        player = PlayerFactory(elo_rating=1500, matches_for_elo=5)
        match = MatchFactory(match_type='tournament', best_of=7)

        k = calculate_k_factor(match, player)
        assert k == 79.2  # base_k * 1.5 * 1.1 * 1.5


@pytest.mark.django_db
class TestExpectedScore:
    """Test Elo expected score calculation"""

    def test_equal_ratings(self):
        """Equal ratings should give 50% expected score"""
        expected = calculate_expected_score(1500, 1500)
        assert expected == pytest.approx(0.5)

    def test_100_point_advantage(self):
        """100 point advantage should give ~64% expected score"""
        expected = calculate_expected_score(1600, 1500)
        assert expected == pytest.approx(0.64, abs=0.01)

    def test_200_point_advantage(self):
        """200 point advantage should give ~76% expected score"""
        expected = calculate_expected_score(1700, 1500)
        assert expected == pytest.approx(0.76, abs=0.01)

    def test_400_point_advantage(self):
        """400 point advantage should give ~91% expected score"""
        expected = calculate_expected_score(1900, 1500)
        assert expected == pytest.approx(0.91, abs=0.01)


@pytest.mark.django_db
class TestEloUpdate:
    """Test full Elo update flow"""

    def test_no_update_without_winner(self):
        """Elo should not update if match has no winner"""
        match = MatchFactory(confirmed=True)
        # No games, so no winner

        player1, player2 = match.side1_players.first(), match.side2_players.first()
        old_elo_1 = player1.elo_rating
        old_elo_2 = player2.elo_rating

        update_player_elo(match)

        player1.refresh_from_db()
        player2.refresh_from_db()

        assert player1.elo_rating == old_elo_1
        assert player2.elo_rating == old_elo_2
        assert EloHistory.objects.count() == 0

    def test_no_update_without_confirmation(self):
        """Elo should not update if match is not confirmed"""
        # Create players with verified emails to prevent auto-confirm
        p1 = PlayerFactory(with_user=True, elo_rating=1500, matches_for_elo=25)
        p2 = PlayerFactory(with_user=True, elo_rating=1500, matches_for_elo=25)
        p1.user.profile.email_verified = True
        p1.user.profile.save()
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        match = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()  # Refresh to get auto-set winner

        old_elo_1 = p1.elo_rating
        old_elo_2 = p2.elo_rating

        update_player_elo(match)

        p1.refresh_from_db()
        p2.refresh_from_db()

        assert p1.elo_rating == old_elo_1
        assert p2.elo_rating == old_elo_2
        assert EloHistory.objects.count() == 0

    def test_winner_gains_points_loser_loses_points(self):
        """Winner should gain Elo, loser should lose Elo"""
        p1 = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        p2 = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(player1=p1, player2=p2, match_type='casual', best_of=5)

        # Player1 wins 3-0
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()

        # Confirm match
        confirm_match(match)

        update_player_elo(match)

        p1.refresh_from_db()
        p2.refresh_from_db()

        # Winner should gain points
        assert p1.elo_rating > 1500
        # Loser should lose points
        assert p2.elo_rating < 1500
        # Sum should be constant (zero-sum)
        assert p1.elo_rating + p2.elo_rating == 3000

    def test_upset_gives_more_points(self):
        """Lower-rated player beating higher-rated player should gain more points"""
        underdog = PlayerFactory(elo_rating=1400, matches_for_elo=25)
        favorite = PlayerFactory(elo_rating=1600, matches_for_elo=25)
        match = MatchFactory(player1=underdog, player2=favorite, match_type='casual', best_of=5)

        # Underdog wins 3-1
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=9, team2_score=11)
        GameFactory(match=match, game_number=4, team1_score=11, team2_score=8)
        match.refresh_from_db()

        confirm_match(match)

        update_player_elo(match)

        underdog.refresh_from_db()
        favorite.refresh_from_db()

        underdog_gain = underdog.elo_rating - 1400
        favorite_loss = 1600 - favorite.elo_rating

        # Underdog should gain more than 16 points (half of K=32)
        assert underdog_gain > 16
        # Favorite should lose more than 16 points
        assert favorite_loss > 16

    def test_expected_win_gives_fewer_points(self):
        """Higher-rated player beating lower-rated player should gain fewer points"""
        favorite = PlayerFactory(elo_rating=1600, matches_for_elo=25)
        underdog = PlayerFactory(elo_rating=1400, matches_for_elo=25)
        match = MatchFactory(player1=favorite, player2=underdog, match_type='casual', best_of=5)

        # Favorite wins 3-0
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()

        confirm_match(match)

        update_player_elo(match)

        favorite.refresh_from_db()
        underdog.refresh_from_db()

        favorite_gain = favorite.elo_rating - 1600

        # Favorite should gain less than 16 points (half of K=32)
        assert favorite_gain < 16

    def test_elo_history_created(self):
        """Elo history records should be created for both players"""
        p1 = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        p2 = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(player1=p1, player2=p2, match_type='casual', best_of=5)

        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()

        confirm_match(match)

        update_player_elo(match)

        # Refresh players to get updated Elo
        p1.refresh_from_db()
        p2.refresh_from_db()

        assert EloHistory.objects.count() == 2

        history_p1 = EloHistory.objects.get(match=match, player=p1)
        history_p2 = EloHistory.objects.get(match=match, player=p2)

        assert history_p1.old_rating == 1500
        assert history_p1.new_rating == p1.elo_rating
        assert history_p1.rating_change == p1.elo_rating - 1500
        assert history_p1.k_factor == 32

        assert history_p2.old_rating == 1500
        assert history_p2.new_rating == p2.elo_rating
        assert history_p2.rating_change == p2.elo_rating - 1500
        assert history_p2.k_factor == 32

    def test_elo_peak_updated(self):
        """Peak Elo should be updated when current Elo exceeds it"""
        player = PlayerFactory(elo_rating=1500, elo_peak=1500, matches_for_elo=25)
        opponent = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(player1=player, player2=opponent, match_type='casual', best_of=5)

        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()

        confirm_match(match)

        update_player_elo(match)

        player.refresh_from_db()

        assert player.elo_peak == player.elo_rating
        assert player.elo_peak > 1500

    def test_matches_for_elo_incremented(self):
        """matches_for_elo should increment for both players"""
        p1 = PlayerFactory(elo_rating=1500, matches_for_elo=10)
        p2 = PlayerFactory(elo_rating=1500, matches_for_elo=20)
        match = MatchFactory(player1=p1, player2=p2, match_type='casual', best_of=5)

        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()

        confirm_match(match)

        update_player_elo(match)

        p1.refresh_from_db()
        p2.refresh_from_db()

        assert p1.matches_for_elo == 11
        assert p2.matches_for_elo == 21

    def test_no_duplicate_elo_update(self):
        """Calling update_player_elo twice should not duplicate history"""
        p1 = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        p2 = PlayerFactory(elo_rating=1500, matches_for_elo=25)
        match = MatchFactory(player1=p1, player2=p2, match_type='casual', best_of=5)

        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()

        confirm_match(match)

        # First update
        update_player_elo(match)
        p1.refresh_from_db()  # Refresh to get updated Elo
        elo_after_first = p1.elo_rating

        # Second update (should be skipped)
        update_player_elo(match)

        p1.refresh_from_db()

        # Elo should not change
        assert p1.elo_rating == elo_after_first
        # Only 2 history records (not 4)
        assert EloHistory.objects.filter(match=match).count() == 2


@pytest.mark.django_db
class TestGetWinProbability:
    def test_equal_ratings_50_50(self):
        p1 = PlayerFactory(elo_rating=1500)
        p2 = PlayerFactory(elo_rating=1500)
        pct1, pct2 = get_win_probability([p1], [p2])
        assert pct1 == 50
        assert pct2 == 50

    def test_higher_rated_favored(self):
        p1 = PlayerFactory(elo_rating=1600)
        p2 = PlayerFactory(elo_rating=1400)
        pct1, pct2 = get_win_probability([p1], [p2])
        assert pct1 > 50
        assert pct2 < 50
        assert pct1 + pct2 == 100

    def test_doubles_uses_side_average(self):
        p1 = PlayerFactory(elo_rating=1600)
        p2 = PlayerFactory(elo_rating=1400)  # avg = 1500
        p3 = PlayerFactory(elo_rating=1500)
        p4 = PlayerFactory(elo_rating=1500)  # avg = 1500
        pct1, pct2 = get_win_probability([p1, p2], [p3, p4])
        assert pct1 == 50
        assert pct2 == 50

    def test_accepts_a_queryset_as_well_as_a_list(self):
        p1 = PlayerFactory(elo_rating=1500, with_user=True)
        p2 = PlayerFactory(elo_rating=1500, with_user=True)
        m = MatchFactory(player1=p1, player2=p2)
        pct1, pct2 = get_win_probability(m.side1_players, m.side2_players)
        assert (pct1, pct2) == (50, 50)

    def test_uses_pre_match_elo_when_history_exists(self):
        p1 = PlayerFactory(elo_rating=1600)
        p2 = PlayerFactory(elo_rating=1400)
        m = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=5)
        m.refresh_from_db()
        confirm_match_silent(m)

        # Clear auto-created history, create with known pre-match ratings
        EloHistory.objects.filter(match=m).delete()
        EloHistory.objects.create(
            match=m, player=p1, old_rating=1500, new_rating=1600,
            rating_change=100, k_factor=32,
        )
        EloHistory.objects.create(
            match=m, player=p2, old_rating=1500, new_rating=1400,
            rating_change=-100, k_factor=32,
        )

        # With match → uses old_rating (both 1500) → 50/50
        pct1, pct2 = get_win_probability([p1], [p2], match=m)
        assert pct1 == 50
        assert pct2 == 50

    def test_empty_sides_return_50_50(self):
        assert get_win_probability([], []) == (50, 50)
        assert get_win_probability([PlayerFactory()], []) == (50, 50)
        assert get_win_probability([], [PlayerFactory()]) == (50, 50)


@pytest.mark.django_db
class TestEloEmptySideGuard:
    def test_singles_match_with_an_empty_side_is_skipped_not_crashed(self):
        """The empty-side guard used to run only for doubles, so a singles
        match with no players on a side raised IndexError.
        """
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p1, player2=p2)
        for n in (1, 2, 3):
            GameFactory(match=m, game_number=n, team1_score=11, team2_score=5)
        m.refresh_from_db()
        confirm_match_silent(m)

        EloHistory.objects.filter(match=m).delete()
        m.participants.filter(side=Side.ONE).delete()
        m.save()
        m.refresh_from_db()
        assert list(m.side1_players) == []

        update_player_elo(m)  # must not raise

        assert EloHistory.objects.filter(match=m).count() == 0


# ---------------------------------------------------------------------------
# KAN-47 (S4.2): the PlayerRating mirror. Everything above is today's
# behaviour and is deliberately left exactly as it was.
# ---------------------------------------------------------------------------

from datetime import date, datetime, timezone as dt_timezone
from unittest import mock

from django.db import IntegrityError

from .. import elo
from ..elo import LEGACY, PROVISIONAL_MATCHES, RatingScope, k_factor, projected_elo_changes
from ..models import PlayerRating, RatingChange
from .conftest import ClubFactory, PlayerRatingFactory, SeasonFactory


def _finish(side1, side2, *, winner=Side.ONE, club=None, when=None, best_of=5, **kwargs):
    """A won match. Players without accounts auto-confirm, so Elo is applied."""
    extra = {"club": club} if club is not None else {}
    if when is not None:
        extra["date_played"] = when
    match = MatchFactory(
        team1_players=list(side1), team2_players=list(side2),
        is_double=len(side1) > 1, best_of=best_of, **extra, **kwargs,
    )
    for n in range(1, best_of // 2 + 2):
        t1, t2 = (11, 5) if winner == Side.ONE else (5, 11)
        GameFactory(match=match, game_number=n, team1_score=t1, team2_score=t2)
    match.refresh_from_db()
    return match


def _all_time(player, fmt):
    return PlayerRating.all_objects.filter(player=player, season=None, format=fmt).first()


def _verified_pair():
    players = [PlayerFactory(with_user=True) for _ in range(2)]
    for p in players:
        p.user.profile.email_verified = True
        p.user.profile.save()
    return players


@pytest.mark.django_db
class TestKFactorHelpers:
    def test_provisional_threshold_is_a_constant(self):
        assert PROVISIONAL_MATCHES == 20

    def test_k_factor_takes_a_match_count(self):
        match = MatchFactory(match_type='tournament', best_of=7)

        assert k_factor(match, 5) == 79.2
        assert k_factor(match, PROVISIONAL_MATCHES) == pytest.approx(52.8)

    def test_calculate_k_factor_delegates_with_the_legacy_count(self):
        player = PlayerFactory(matches_for_elo=19)
        match = MatchFactory()

        assert calculate_k_factor(match, player) == k_factor(match, 19)


@pytest.mark.django_db
class TestFormatRatingsAreSeparate:
    def test_a_singles_match_changes_only_singles(self):
        p1, p2 = PlayerFactory(), PlayerFactory()

        match = _finish([p1], [p2])

        assert not PlayerRating.all_objects.filter(format="doubles").exists()
        r1, r2 = _all_time(p1, "singles"), _all_time(p2, "singles")
        h1 = EloHistory.objects.get(match=match, player=p1)
        assert (r1.rating, r1.peak, r1.matches) == (h1.new_rating, h1.new_rating, 1)
        assert r2.rating == 1500 + EloHistory.objects.get(match=match, player=p2).rating_change
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_a_doubles_match_changes_only_doubles_with_the_same_delta_per_side(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))

        match = _finish([a, b], [c, d])

        assert not PlayerRating.all_objects.filter(format="singles").exists()
        changes = {rc.player_id: rc for rc in RatingChange.objects.filter(match=match)}
        assert len(changes) == 4
        assert changes[a.pk].rating_change == changes[b.pk].rating_change > 0
        assert changes[c.pk].rating_change == changes[d.pk].rating_change < 0
        # Same delta as the legacy write, since everyone started level.
        assert changes[a.pk].rating_change == EloHistory.objects.get(match=match, player=a).rating_change

    def test_a_doubles_only_player_is_unrated_in_singles(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        _finish([a, b], [c, d])

        assert _all_time(a, "singles") is None
        assert _all_time(a, "doubles") is not None

    def test_legacy_fields_still_combine_both_formats(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        _finish([a], [b])
        _finish([a, c], [b, d])

        a.refresh_from_db()
        assert a.matches_for_elo == 2
        assert _all_time(a, "singles").matches == 1
        assert _all_time(a, "doubles").matches == 1

    def test_format_experience_is_the_all_time_count_in_that_format(self):
        """Established in legacy (25 matches) but new to singles: provisional
        K for the singles rating, normal K for the legacy one."""
        p1 = PlayerFactory(matches_for_elo=25)
        p2 = PlayerFactory(matches_for_elo=25)

        match = _finish([p1], [p2])

        assert EloHistory.objects.get(match=match, player=p1).k_factor == 32
        assert RatingChange.objects.get(match=match, player=p1).k_factor == 48

    def test_the_preview_for_a_scope_is_what_gets_written(self):
        p1, p2 = _verified_pair()
        PlayerRatingFactory(player=p1, rating=1620, matches=30)
        match = _finish([p1], [p2], match_type="tournament")
        scope = RatingScope(None, "singles")
        preview = projected_elo_changes(match, scope)

        confirm_match(match)

        written = {rc.player_id: rc for rc in RatingChange.objects.filter(match=match)}
        assert written[p1.pk].rating_change == preview.side1_change
        assert written[p1.pk].k_factor == preview.side1_k
        assert written[p2.pk].rating_change == preview.side2_change
        assert written[p1.pk].old_rating == 1620

    def test_legacy_scope_is_the_default(self):
        p1, p2 = _verified_pair()
        match = _finish([p1], [p2])

        assert projected_elo_changes(match) == projected_elo_changes(match, LEGACY)


@pytest.mark.django_db
class TestSeasonRatings:
    def _season_q1(self, club):
        return SeasonFactory(club=club, starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))

    def test_a_match_in_a_season_writes_all_time_and_season_rows(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        match = _finish([p1], [p2], club=club, when=datetime(2026, 2, 1, tzinfo=dt_timezone.utc))

        rows = PlayerRating.all_objects.filter(player=p1)
        assert {(r.season_id, r.format) for r in rows} == {(None, "singles"), (season.pk, "singles")}
        season_row = rows.get(season=season)
        assert season_row.club_id == club.pk
        assert season_row.matches == 1
        assert RatingChange.objects.filter(match=match).count() == 4

    def test_first_season_rating_is_seeded_by_soft_reset_before_this_match(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)
        PlayerRatingFactory(player=p1, rating=1700, peak=1750, matches=40)

        match = _finish([p1], [p2], club=club, when=datetime(2026, 2, 1, tzinfo=dt_timezone.utc))

        season_change = RatingChange.objects.get(match=match, player=p1, rating__season=season)
        all_time_change = RatingChange.objects.get(match=match, player=p1, rating__season=None)
        assert all_time_change.old_rating == 1700
        assert season_change.old_rating == 1600  # 1500 + 0.5 * (1700 - 1500)
        # Experience is the all-time format count, so both scopes share a K.
        assert season_change.k_factor == all_time_change.k_factor == 32
        assert PlayerRating.all_objects.get(player=p1, season=season).peak == max(1600, season_change.new_rating)

    def test_an_unrated_player_starts_the_season_at_1500(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        match = _finish([p1], [p2], club=club, when=datetime(2026, 2, 1, tzinfo=dt_timezone.utc))

        assert RatingChange.objects.get(match=match, player=p2, rating__season=season).old_rating == 1500

    def test_a_second_season_match_continues_the_season_row(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)
        _finish([p1], [p2], club=club, when=datetime(2026, 2, 1, tzinfo=dt_timezone.utc))
        after_first = PlayerRating.all_objects.get(player=p1, season=season).rating

        second = _finish([p1], [p2], club=club, when=datetime(2026, 2, 2, tzinfo=dt_timezone.utc))

        assert RatingChange.objects.get(match=second, player=p1, rating__season=season).old_rating == after_first
        assert PlayerRating.all_objects.get(player=p1, season=season).matches == 2

    def test_a_match_outside_every_season_counts_all_time_only(self):
        club = ClubFactory()
        self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        match = _finish([p1], [p2], club=club, when=datetime(2026, 4, 1, tzinfo=dt_timezone.utc))

        assert not PlayerRating.all_objects.filter(season__isnull=False).exists()
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_another_clubs_season_does_not_apply(self):
        self._season_q1(ClubFactory())
        club = ClubFactory()
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        _finish([p1], [p2], club=club, when=datetime(2026, 2, 1, tzinfo=dt_timezone.utc))

        assert not PlayerRating.all_objects.filter(season__isnull=False).exists()


@pytest.mark.django_db
class TestRatingWritesAreRaceSafe:
    def test_a_confirmer_that_loses_the_race_writes_nothing_and_does_not_raise(self):
        """Both confirmers pass the 'already applied?' guard; the second one's
        inserts hit the unique constraints, the whole block rolls back, and it
        treats that as already applied."""
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = _finish([p1], [p2])
        p1.refresh_from_db()
        before = (p1.elo_rating, _all_time(p1, "singles").rating,
                  EloHistory.objects.count(), RatingChange.objects.count())

        # Guard says "not yet" (we raced), the post-failure re-check says "done".
        with mock.patch.object(elo, "_already_applied", side_effect=[False, True]):
            update_player_elo(match)

        p1.refresh_from_db()
        assert (p1.elo_rating, _all_time(p1, "singles").rating,
                EloHistory.objects.count(), RatingChange.objects.count()) == before

    def test_a_conflict_that_is_not_this_match_is_retried(self):
        """Two *different* matches creating the same new rating row: the loser
        must retry, not mistake it for having already applied."""
        p1, p2 = _verified_pair()
        match = _finish([p1], [p2])  # verified players: not confirmed, no Elo yet
        confirm_match_silent(match)  # confirmed without firing the pipeline
        match.invalidate_confirmation_state()
        real_apply = elo._apply_match
        calls = []

        def flaky(m):
            calls.append(m.pk)
            if len(calls) == 1:
                raise IntegrityError("unique constraint on a fresh all-time row")
            return real_apply(m)

        with mock.patch.object(elo, "_apply_match", side_effect=flaky):
            update_player_elo(match)

        assert len(calls) == 2
        assert EloHistory.objects.filter(match=match).count() == 2
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_calling_twice_writes_ratings_once(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = _finish([p1], [p2])

        update_player_elo(match)
        update_player_elo(match)

        assert RatingChange.objects.filter(match=match).count() == 2
        assert _all_time(p1, "singles").matches == 1

"""
Unit tests for Elo rating calculation logic.
"""

import pytest
from django.contrib.auth.models import User

from ..elo import calculate_k_factor, calculate_expected_score, update_player_elo
from ..models import Player, Match, Game, EloHistory
from .conftest import PlayerFactory, MatchFactory, GameFactory, UserFactory, confirm_match


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

        player1, player2 = match.team1.players.first(), match.team2.players.first()
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

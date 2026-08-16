"""
Tests for management commands.
"""

import pytest
from io import StringIO
from django.core.management import call_command

from ..models import Player, Match, Game, EloHistory
from .conftest import PlayerFactory, MatchFactory, GameFactory, confirm_match, confirm_match_silent


@pytest.mark.django_db
class TestRecalculateElo:
    """Test recalculate_elo management command"""

    def test_dry_run_no_changes(self):
        """Dry run should not make any changes"""
        p1 = PlayerFactory(elo_rating=1600, matches_for_elo=10)
        p2 = PlayerFactory(elo_rating=1400, matches_for_elo=10)

        match = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        confirm_match(match)

        # Save Elo values AFTER confirmation (signal will have updated them)
        p1.refresh_from_db()
        p2.refresh_from_db()
        elo_p1_before = p1.elo_rating
        elo_p2_before = p2.elo_rating
        matches_p1_before = p1.matches_for_elo
        matches_p2_before = p2.matches_for_elo

        out = StringIO()
        call_command('recalculate_elo', '--dry-run', stdout=out)

        p1.refresh_from_db()
        p2.refresh_from_db()

        # Elo should not change from what it was before the command
        assert p1.elo_rating == elo_p1_before
        assert p2.elo_rating == elo_p2_before
        assert p1.matches_for_elo == matches_p1_before
        assert p2.matches_for_elo == matches_p2_before

        # Output should mention dry run
        output = out.getvalue()
        assert 'DRY RUN' in output
        assert 'Found 1 confirmed matches' in output

    def test_recalculate_resets_all_players(self):
        """Command should reset all players to 1500 before recalculating"""
        p1 = PlayerFactory(elo_rating=1700, elo_peak=1750, matches_for_elo=50)
        p2 = PlayerFactory(elo_rating=1300, elo_peak=1500, matches_for_elo=40)

        match = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        # Use silent confirm to avoid triggering Elo updates before the command
        confirm_match_silent(match)

        call_command('recalculate_elo', stdout=StringIO())

        p1.refresh_from_db()
        p2.refresh_from_db()

        # Elo should be recalculated from 1500 base (not from 1700/1300)
        # Winner should be slightly above 1500
        assert p1.elo_rating > 1500
        assert p1.elo_rating < 1550
        # Loser should be slightly below 1500
        assert p2.elo_rating < 1500
        assert p2.elo_rating > 1450

        # matches_for_elo should be 1 (only one match)
        assert p1.matches_for_elo == 1
        assert p2.matches_for_elo == 1

    def test_recalculate_chronological_order(self):
        """Command should process matches in chronological order"""
        from datetime import datetime, timedelta
        from django.utils import timezone

        p1 = PlayerFactory(elo_rating=1500, matches_for_elo=0)
        p2 = PlayerFactory(elo_rating=1500, matches_for_elo=0)

        # Create 3 matches over 3 days
        base_date = timezone.make_aware(datetime(2024, 1, 1, 12, 0, 0))

        for i in range(3):
            match = MatchFactory(
                player1=p1,
                player2=p2,
                date_played=base_date + timedelta(days=i)
            )
            # P1 wins all matches
            GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
            GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
            GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
            match.refresh_from_db()
            # Use silent confirm to avoid triggering Elo updates before the command
            confirm_match_silent(match)

        call_command('recalculate_elo', stdout=StringIO())

        p1.refresh_from_db()
        p2.refresh_from_db()

        # P1 should have gained Elo progressively (3 wins)
        assert p1.elo_rating > 1540  # ~16 points per win
        # P2 should have lost Elo progressively (3 losses)
        assert p2.elo_rating < 1460

        # 3 matches counted
        assert p1.matches_for_elo == 3
        assert p2.matches_for_elo == 3

        # 6 history records (2 per match)
        assert EloHistory.objects.count() == 6

    def test_recalculate_clears_existing_history(self):
        """Command should delete existing Elo history before recalculating"""
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)

        # Create fake history
        EloHistory.objects.create(
            match=match,
            player=p1,
            old_rating=1500,
            new_rating=1516,
            rating_change=16,
            k_factor=32.0,
        )

        assert EloHistory.objects.count() == 1

        # Create real match
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        confirm_match(match)

        out = StringIO()
        call_command('recalculate_elo', stdout=out)

        # Old history should be deleted, new history created
        assert EloHistory.objects.count() == 2

        output = out.getvalue()
        assert 'Deleted 1 existing Elo history records' in output

    def test_skip_unconfirmed_matches(self):
        """Command should skip matches without both confirmations"""
        # Create players with verified emails to prevent auto-confirm
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        p1.user.profile.email_verified = True
        p1.user.profile.save()
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        # Confirmed match
        match1 = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match1, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match1, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match1, game_number=3, team1_score=11, team2_score=9)
        match1.refresh_from_db()
        confirm_match(match1)

        # Unconfirmed match (will NOT auto-confirm because players are verified)
        match2 = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match2, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match2, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match2, game_number=3, team1_score=11, team2_score=9)
        match2.refresh_from_db()
        # No confirmations

        out = StringIO()
        call_command('recalculate_elo', stdout=out)

        output = out.getvalue()
        # Only 1 match should be processed
        assert 'Found 1 confirmed matches' in output

        # Only 2 history records (from confirmed match)
        # Note: There might be 4 if the first match was auto-confirmed, but we manually confirmed it
        # so there should only be 2 from the recalculate command
        assert EloHistory.objects.count() == 2

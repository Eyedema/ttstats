from django.contrib.auth.models import User
from django.test import TestCase
from django.core import mail

from pingpong.models import Player, Match, Game, Team, UserProfile


class UserProfileSignalTest(TestCase):
    """Tests for user profile creation signal"""

    def test_profile_created_on_user_creation(self):
        """Test that UserProfile is created when User is created"""
        user = User.objects.create_user(
            username="newuser", email="new@example.com", password="password123"
        )

        # Profile should exist
        self.assertTrue(hasattr(user, "profile"))
        self.assertIsInstance(user.profile, UserProfile)

        # Verification token should be created
        self.assertNotEqual(user.profile.email_verification_token, "")
        self.assertIsNotNone(user.profile.email_verification_sent_at)


class MatchCompletionSignalTest(TestCase):
    """Tests for match completion signal (winner set)"""

    def setUp(self):
        """Set up test data"""
        self.user1 = User.objects.create_user(
            username="player1", email="p1@example.com", password="pass"
        )
        self.user2 = User.objects.create_user(
            username="player2", email="p2@example.com", password="pass"
        )
        self.user3 = User.objects.create_user(
            username="player3", email="p3@example.com", password="pass"
        )
        self.user4 = User.objects.create_user(
            username="player4", email="p4@example.com", password="pass"
        )
        self.player1 = Player.objects.create(user=self.user1, name="Player 1")
        self.player2 = Player.objects.create(user=self.user2, name="Player 2")
        self.player3 = Player.objects.create(user=self.user3, name="Player 3")
        self.player4 = Player.objects.create(user=self.user4, name="Player 4")

        self.team1 = Team.objects.create()
        self.team1.players.set([self.player1])
        self.team1.save()

        self.team2 = Team.objects.create()
        self.team2.players.set([self.player2])
        self.team2.save()

        self.team_double1 = Team.objects.create()
        self.team_double1.players.set([self.player1, self.player2])
        self.team_double1.save()

        self.team_double2 = Team.objects.create()
        self.team_double2.players.set([self.player3, self.player4])
        self.team_double2.save()

        # Clear any emails from user creation
        mail.outbox = []

    def test_auto_confirm_for_unverified_users_singles(self):
        """Test match is auto-confirmed when users are unverified"""
        # Ensure users are not verified
        self.user1.profile.email_verified = False
        self.user1.profile.save()
        self.user2.profile.email_verified = False
        self.user2.profile.save()

        # Create match without winner
        match = Match.objects.create(
            team1=self.team1, team2=self.team2, best_of=5
        )

        # Add games to set winner (triggers signal)
        Game.objects.create(
            match=match, game_number=1, team1_score=11, team2_score=5
        )
        Game.objects.create(
            match=match, game_number=2, team1_score=11, team2_score=9
        )
        Game.objects.create(
            match=match, game_number=3, team1_score=11, team2_score=7
        )

        match.refresh_from_db()

        # Should be auto-confirmed
        self.assertTrue(match.team1_confirmed)
        self.assertTrue(match.team2_confirmed)

        # No emails should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_auto_confirm_for_unverified_users_doubles(self):
        """Test match is auto-confirmed when users are unverified"""
        # Ensure users are not verified
        self.user1.profile.email_verified = False
        self.user1.profile.save()
        self.user2.profile.email_verified = False
        self.user2.profile.save()
        self.user3.profile.email_verified = False
        self.user3.profile.save()
        self.user4.profile.email_verified = False
        self.user4.profile.save()

        # Create match without winner
        match = Match.objects.create(
            team1=self.team_double1, team2=self.team_double2, best_of=5
        )

        # Add games to set winner (triggers signal)
        Game.objects.create(
            match=match, game_number=1, team1_score=11, team2_score=5
        )
        Game.objects.create(
            match=match, game_number=2, team1_score=11, team2_score=9
        )
        Game.objects.create(
            match=match, game_number=3, team1_score=11, team2_score=7
        )

        match.refresh_from_db()

        # Should be auto-confirmed
        self.assertTrue(match.team1_confirmed)
        self.assertTrue(match.team2_confirmed)

        # No emails should be sent
        self.assertEqual(len(mail.outbox), 0)

    def test_send_emails_for_verified_users_singles(self):
        """Test confirmation emails sent when both users are verified"""
        # Mark users as verified
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()

        # Create match
        match = Match.objects.create(
            team1=self.team1, team2=self.team2, best_of=5
        )

        # Set winner (triggers signal)
        Game.objects.create(
            match=match, game_number=1, team1_score=11, team2_score=5
        )
        Game.objects.create(
            match=match, game_number=2, team1_score=11, team2_score=9
        )
        Game.objects.create(
            match=match, game_number=3, team1_score=11, team2_score=7
        )

        match.refresh_from_db()

        self.assertFalse(match.team1_confirmed)
        self.assertFalse(match.team2_confirmed)

        # Two emails should be sent (one to each player)
        self.assertEqual(len(mail.outbox), 2)

        # Check emails were sent to correct recipients
        recipients = [email.to[0] for email in mail.outbox]
        self.assertIn(self.user1.email, recipients)
        self.assertIn(self.user2.email, recipients)

    def test_send_emails_for_verified_users_doubles(self):
        """Test confirmation emails sent when both teams are verified"""
        # Mark users as verified
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = True
        self.user4.profile.save()

        # Create match
        match = Match.objects.create(
            team1=self.team_double1, team2=self.team_double2, best_of=5
        )

        # Set winner (triggers signal)
        Game.objects.create(
            match=match, game_number=1, team1_score=11, team2_score=5
        )
        Game.objects.create(
            match=match, game_number=2, team1_score=11, team2_score=9
        )
        Game.objects.create(
            match=match, game_number=3, team1_score=11, team2_score=7
        )

        match.refresh_from_db()

        self.assertFalse(match.team1_confirmed)
        self.assertFalse(match.team2_confirmed)

        # Two emails should be sent (one to each player)
        self.assertEqual(len(mail.outbox), 4)

        # Check emails were sent to correct recipients
        recipients = [email.to[0] for email in mail.outbox]
        self.assertIn(self.user1.email, recipients)
        self.assertIn(self.user2.email, recipients)
        self.assertIn(self.user3.email, recipients)
        self.assertIn(self.user4.email, recipients)

    def test_no_emails_for_one_verified_one_unverified_singles(self):
        """Test auto-confirm when one player is verified, one is not"""
        # Only player1 verified
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = False
        self.user2.profile.save()

        match = Match.objects.create(
            team1=self.team1, team2=self.team2, best_of=5
        )

        Game.objects.create(
            match=match, game_number=1, team1_score=11, team2_score=5
        )
        Game.objects.create(
            match=match, game_number=2, team1_score=11, team2_score=9
        )
        Game.objects.create(
            match=match, game_number=3, team1_score=11, team2_score=7
        )

        match.refresh_from_db()

        self.assertTrue(match.team1_confirmed)
        self.assertTrue(match.team2_confirmed)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_emails_for_one_team_verified_one_team_unverified_doubles(self):
        """Test auto-confirm when one team is verified, one is not"""
        # Only player1 verified
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = False
        self.user3.profile.save()
        self.user4.profile.email_verified = False
        self.user4.profile.save()

        match = Match.objects.create(
            team1=self.team_double1, team2=self.team_double2, best_of=5
        )

        Game.objects.create(
            match=match, game_number=1, team1_score=11, team2_score=5
        )
        Game.objects.create(
            match=match, game_number=2, team1_score=11, team2_score=9
        )
        Game.objects.create(
            match=match, game_number=3, team1_score=11, team2_score=7
        )

        match.confirmations.set([self.player1, self.player2])

        match.refresh_from_db()

        self.assertTrue(match.team1_confirmed)
        self.assertTrue(match.team2_confirmed)
        self.assertEqual(len(mail.outbox), 0)
    
    def test_match_saved_without_winner(self):
        """Test signal doesn't trigger when match is saved without setting winner"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()

        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            best_of=5,
        )

        match.notes = "Some notes"
        match.save()

        self.assertEqual(len(mail.outbox), 0)

    def test_signal_not_triggered_when_winner_unchanged(self):
        """Test signal doesn't trigger when updating match without changing winner"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()

        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            winner=self.team1,  # Already has winner
        )

        # Update match without changing winner
        match.notes = "Updated notes"
        match.save()

        # No emails should be sent
        self.assertEqual(len(mail.outbox), 0)

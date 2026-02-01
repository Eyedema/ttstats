from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta, datetime

from pingpong.models import Location, Player, Match, Game, UserProfile, Team, MatchConfirmation


class LocationModelTest(TestCase):
    """Tests for the Location model"""
    
    def test_location_creation(self):
        """Test creating a location"""
        location = Location.objects.create(
            name="Test",
            address="123",
            notes="topperia maxima"
        )
        self.assertEqual(str(location), "Test")
        self.assertEqual(location.name, "Test")
        self.assertEqual(location.address, "123")
        self.assertEqual(location.notes, "topperia maxima")
    
    def test_location_ordering(self):
        """Test locations are ordered by name"""
        Location.objects.create(name="Z")
        Location.objects.create(name="A")
        Location.objects.create(name="B")
        
        locations = list(Location.objects.all())
        self.assertEqual(locations[0].name, "A")
        self.assertEqual(locations[1].name, "B")
        self.assertEqual(locations[2].name, "Z")


class PlayerModelTest(TestCase):
    """Tests for the Player model"""
    
    def setUp(self):
        """Set up test data"""
        self.user = User.objects.create_user(
            username="Marco Tennistavolo",
            email="marco@tennistavolo.com",
            password="testpass123"
        )
    
    def test_player_creation_with_user(self):
        """Test creating a player linked to a user"""
        player = Player.objects.create(
            user=self.user,
            name="Test Player",
            nickname="Testy",
            playing_style="normal",
            notes="notes"
        )
        self.assertEqual(player.user, self.user)
        self.assertEqual(player.name, "Test Player")
        self.assertEqual(str(player), "Testy")
        self.assertEqual(player.notes, "notes")
    
    def test_player_creation_without_user(self):
        """Test creating a standalone player (no user account)"""
        player = Player.objects.create(
            name="Guest Player",
            playing_style="hard_rubber"
        )
        self.assertIsNone(player.user)
    
    def test_player_str_without_nickname(self):
        """Test __str__ returns name when no nickname"""
        player = Player.objects.create(name="pippo")
        self.assertEqual(str(player), "pippo")
    
    def test_user_can_edit_own_player(self):
        """Test user can edit their own player profile"""
        player = Player.objects.create(user=self.user, name="Test")
        self.assertTrue(player.user_can_edit(self.user))
    
    def test_user_cannot_edit_other_player(self):
        """Test user cannot edit another user's player"""
        other_user = User.objects.create_user(username="other", password="pass")
        other_player = Player.objects.create(user=other_user, name="Other")
        self.assertFalse(other_player.user_can_edit(self.user))
    
    def test_staff_can_edit_any_player(self):
        """Test staff users can edit any player"""
        staff_user = User.objects.create_user(
            username="staff",
            password="pass",
            is_staff=True
        )
        player = Player.objects.create(user=self.user, name="Test")
        self.assertTrue(player.user_can_edit(staff_user))
    
    def test_unauthenticated_cannot_edit(self):
        """Test None/anonymous user cannot edit"""
        player = Player.objects.create(user=self.user, name="Test")
        self.assertFalse(player.user_can_edit(None))
    
    def test_player_ordering(self):
        """Test players are ordered by name"""
        Player.objects.create(name="Z")
        Player.objects.create(name="A")
        Player.objects.create(name="B")
        
        players = list(Player.objects.all())
        self.assertEqual(players[0].name, "A")
        self.assertEqual(players[1].name, "B")
        self.assertEqual(players[2].name, "Z")


class TeamModelTest(TestCase):
    """Tests for the Team model"""
    def setUp(self):
        self.user1 = User.objects.create_user(username="player1", password="pass")
        self.user2 = User.objects.create_user(username="player2", password="pass")
        self.user3 = User.objects.create_user(username="player3", password="pass")
        self.user4 = User.objects.create_user(username="player4", password="pass")

        self.player1 = Player.objects.create(user=self.user1, name="Player One")
        self.player2 = Player.objects.create(user=self.user2, name="Player Two")
        self.player3 = Player.objects.create(user=self.user3, name="Player Three")
        self.player4 = Player.objects.create(user=self.user4, name="Player Four")

        self.team1 = Team.objects.create(name="The chopper")
        self.team1.players.set([self.player1])
        self.team2 = Team.objects.create(name="The blocker")
        self.team2.players.set([self.player2])
        self.team3 = Team.objects.create()
        self.team3.players.set([self.player1, self.player2, self.player3, self.player4])

    def test_teams_custom_names(self):
        self.assertEqual(f"{self.team1}", "The chopper")
        self.assertEqual(f"{self.team2}", "The blocker")

    def test_teams_name_with_more_than_two_players(self):
        self.assertEqual(f"{self.team3}", "Player Four and Player One (+2)")


class MatchModelTest(TestCase):
    """Tests for the Match model"""
    
    def setUp(self):
        """Set up test data"""
        self.user1 = User.objects.create_user(username="player1", password="pass")
        self.user2 = User.objects.create_user(username="player2", password="pass")
        self.user3 = User.objects.create_user(username="player3", password="pass")
        self.user4 = User.objects.create_user(username="player4", password="pass")

        self.player1 = Player.objects.create(user=self.user1, name="Player One")
        self.player2 = Player.objects.create(user=self.user2, name="Player Two")
        self.player3 = Player.objects.create(user=self.user3, name="Player Three")
        self.player4 = Player.objects.create(user=self.user4, name="Player Four")

        self.location = Location.objects.create(name="location1")

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
    
    def test_singles_match_creation(self):
        """Test creating a match"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = True
        self.user4.profile.save()

        date_played = timezone.now() - timedelta(days=1)
        match = Match.objects.create(
            is_double=False,
            team1=self.team1,
            team2=self.team2,
            date_played=date_played,
            match_type="casual",
            best_of=5,
            location=self.location,
            notes="note"
        )
        self.assertEqual(match.is_double, False)
        self.assertEqual(match.team1, self.team1)
        self.assertEqual(match.team2, self.team2)
        self.assertEqual(match.best_of, 5)
        self.assertEqual(match.location, self.location)
        self.assertEqual(match.match_type, "casual")
        self.assertEqual(match.notes, "note")
        self.assertEqual(match.date_played, date_played)
        self.assertIsNone(match.winner)
        self.assertFalse(match.team1_confirmed)
        self.assertFalse(match.team2_confirmed)

    def test_doubles_match_creation(self):
        """Test creating a match"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = True
        self.user4.profile.save()

        date_played = timezone.now() - timedelta(days=1)
        match = Match.objects.create(
            is_double=True,
            team1=self.team_double1,
            team2=self.team_double2,
            date_played=date_played,
            match_type="casual",
            best_of=5,
            location=self.location,
            notes="note"
        )
        self.assertEqual(match.is_double, True)
        self.assertEqual(match.team1, self.team_double1)
        self.assertEqual(match.team2, self.team_double2)
        self.assertEqual(match.best_of, 5)
        self.assertEqual(match.location, self.location)
        self.assertEqual(match.match_type, "casual")
        self.assertEqual(match.notes, "note")
        self.assertEqual(match.date_played, date_played)
        self.assertIsNone(match.winner)
        self.assertFalse(match.team1_confirmed)
        self.assertFalse(match.team2_confirmed)

    def test_singles_match_str_representation(self):
        """Test match string representation"""
        match = Match.objects.create(
            is_double=False,
            team1=self.team1,
            team2=self.team2,
            date_played=timezone.now()
        )
        expected = f"{self.player1} vs {self.player2} - {match.date_played.date()}"
        self.assertEqual(str(match), expected)

    def test_doubles_match_str_representation(self):
        """Test match string representation"""
        match = Match.objects.create(
            is_double=True,
            team1=self.team_double1,
            team2=self.team_double2,
            date_played=timezone.now()
        )
        # Below 3 and 4 are inverted because of the alphabetical order ("Player Four and Player Three")
        expected = f"{self.player1} and {self.player2} vs {self.player4} and {self.player3} - {match.date_played.date()}"
        self.assertEqual(str(match), expected)

    # TODO: add tests for more than 2 players?
    
    def test_singles_match_confirmed_property(self):
        """Test match_confirmed property"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()

        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        # Initially not confirmed
        self.assertFalse(match.match_confirmed)
        
        # Only player1 confirmed
        match.confirmations.set([self.player1])
        match.refresh_from_db()
        self.assertFalse(match.match_confirmed)
        
        match.confirmations.set([self.player1, self.player2])
        match.refresh_from_db()
        self.assertTrue(match.match_confirmed)

    def test_doubles_match_confirmed_property(self):
        """Test match_confirmed property"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = True
        self.user4.profile.save()

        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2
        )
        # Initially not confirmed
        self.assertFalse(match.match_confirmed)

        # Only player1 confirmed
        match.confirmations.set([self.player1])
        match.refresh_from_db()
        self.assertFalse(match.match_confirmed)

        # Only team 1 confirmed
        match.confirmations.set([self.player1, self.player2])
        match.refresh_from_db()
        self.assertFalse(match.match_confirmed)

        # Only team 1 + player 3 confirmed
        match.confirmations.set([self.player1, self.player2, self.player3])
        match.refresh_from_db()
        self.assertFalse(match.match_confirmed)

        # Everyone confirmed
        match.confirmations.set([self.player1, self.player2, self.player3, self.player4])
        match.refresh_from_db()
        self.assertTrue(match.match_confirmed)

    def test_doubles_team_match_confirmed_property_mixed_players(self):
        """Test match_confirmed property"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = False
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = False
        self.user4.profile.save()

        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2
        )
        # Initially not confirmed
        self.assertFalse(match.match_confirmed)

        # Only player1 confirmed
        match.confirmations.set([self.player1])
        match.refresh_from_db()
        self.assertTrue(match.team1_confirmed)
        self.assertFalse(match.match_confirmed)

        # Everyone confirmed
        match.confirmations.set([self.player1, self.player3])
        match.refresh_from_db()
        self.assertTrue(match.team1_confirmed)
        self.assertTrue(match.team2_confirmed)
        self.assertTrue(match.match_confirmed)
    
    def test_player_scores_empty_match(self):
        """Test player scores for match with no games"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        self.assertEqual(match.team1_score, 0)
        self.assertEqual(match.team2_score, 0)
    
    def test_player_scores_with_games(self):
        """Test player scores calculated from games"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            best_of=5
        )
        # Player 1 wins 2 games
        Game.objects.create(match=match, game_number=1, team1_score=11, team2_score=5)
        Game.objects.create(match=match, game_number=2, team1_score=11, team2_score=9)
        # Player 2 wins 1 game
        Game.objects.create(match=match, game_number=3, team1_score=8, team2_score=11)
        
        match.refresh_from_db()
        self.assertEqual(match.team1_score, 2)
        self.assertEqual(match.team2_score, 1)
    
    def test_singles_auto_determine_winner(self):
        """Test winner is automatically determined when enough games are won"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            best_of=5
        )
        
        # Player1 wins
        Game.objects.create(match=match, game_number=1, team1_score=11, team2_score=5)
        Game.objects.create(match=match, game_number=2, team1_score=11, team2_score=9)
        Game.objects.create(match=match, game_number=3, team1_score=11, team2_score=7)
        
        match.refresh_from_db()
        self.assertEqual(match.winner, self.team1)


        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            best_of=5
        )
        
        # Player2 wins
        Game.objects.create(match=match, game_number=1, team1_score=0, team2_score=11)
        Game.objects.create(match=match, game_number=2, team1_score=0, team2_score=11)
        Game.objects.create(match=match, game_number=3, team1_score=0, team2_score=11)
        
        match.refresh_from_db()
        self.assertEqual(match.winner, self.team2)

    def test_doubles_auto_determine_winner(self):
        """Test winner is automatically determined when enough games are won"""
        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2,
            best_of=5
        )

        # Player1 wins
        Game.objects.create(match=match, game_number=1, team1_score=11, team2_score=5)
        Game.objects.create(match=match, game_number=2, team1_score=11, team2_score=9)
        Game.objects.create(match=match, game_number=3, team1_score=11, team2_score=7)

        match.refresh_from_db()
        self.assertEqual(match.winner, self.team_double1)

        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2,
            best_of=5
        )

        # Player2 wins
        Game.objects.create(match=match, game_number=1, team1_score=0, team2_score=11)
        Game.objects.create(match=match, game_number=2, team1_score=0, team2_score=11)
        Game.objects.create(match=match, game_number=3, team1_score=0, team2_score=11)

        match.refresh_from_db()
        self.assertEqual(match.winner, self.team_double2)
    
    def test_should_auto_confirm_singles_unverified_players(self):
        """Test match auto-confirms when players have unverified emails"""
        # Ensure profiles exist with unverified emails
        self.user1.profile.email_verified = False
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            winner=self.team1
        )
        
        self.assertTrue(match.should_auto_confirm())
    
    def test_should_not_auto_confirm_singles_verified_players(self):
        """Test match doesn't auto-confirm when both players are verified"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()

        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            winner=self.team1
        )
        
        self.assertFalse(match.should_auto_confirm())

    def test_should_auto_confirm_doubles_unverified_players(self):
        """Test match auto-confirms when both opponent team players have unverified emails"""
        # Ensure profiles exist with unverified emails
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = False
        self.user3.profile.save()
        self.user4.profile.email_verified = False
        self.user4.profile.save()

        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2,
            winner=self.team_double1
        )

        self.assertTrue(match.should_auto_confirm())

    def test_should_not_auto_confirm_doubles_verified_players(self):
        """Test match doesn't auto-confirm when all players are verified"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = True
        self.user4.profile.save()

        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2,
            winner=self.team_double1
        )

        self.assertFalse(match.should_auto_confirm())
    
    def test_get_unverified_players_one_unverified(self):
        """Test getting list of unverified players"""
        self.user1.profile.email_verified = False
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        
        unverified = match.get_unverified_players()
        self.assertEqual(len(unverified), 1)
        self.assertIn(self.player1, unverified)
        self.assertNotIn(self.player2, unverified)
    
    def test_get_unverified_players_both_unverified(self):
        """Test getting list when both players are unverified"""
        self.user1.profile.email_verified = False
        self.user1.profile.save()
        self.user2.profile.email_verified = False
        self.user2.profile.save()
        
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        
        unverified = match.get_unverified_players()
        self.assertEqual(len(unverified), 2)
        self.assertIn(self.player1, unverified)
        self.assertIn(self.player2, unverified)

    def test_get_unverified_players_mixed_unverified(self):
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = False
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = False
        self.user4.profile.save()

        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2
        )

        unverified = match.get_unverified_players()
        self.assertEqual(len(unverified), 2)
        self.assertIn(self.player2, unverified)
        self.assertIn(self.player4, unverified)
    
    def test_get_unverified_players_none_unverified(self):
        """Test getting list when both players are verified"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        
        unverified = match.get_unverified_players()
        self.assertEqual(len(unverified), 0)

    def test_get_unverified_players_doubles_none_unverified(self):
        """Test getting list when both players are verified"""
        self.user1.profile.email_verified = True
        self.user1.profile.save()
        self.user2.profile.email_verified = True
        self.user2.profile.save()
        self.user3.profile.email_verified = True
        self.user3.profile.save()
        self.user4.profile.email_verified = True
        self.user4.profile.save()

        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2
        )

        unverified = match.get_unverified_players()
        self.assertEqual(len(unverified), 0)
    
    def test_user_can_edit_own_singles_match(self):
        """Test user can edit matches they participate in"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        self.assertTrue(match.user_can_edit(self.user1))
        self.assertTrue(match.user_can_edit(self.user2))

    def test_user_can_edit_own_doubles_match(self):
        """Test user can edit matches they participate in"""
        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2
        )
        self.assertTrue(match.user_can_edit(self.user1))
        self.assertTrue(match.user_can_edit(self.user2))
        self.assertTrue(match.user_can_edit(self.user3))
        self.assertTrue(match.user_can_edit(self.user4))

    def test_user_can_edit_attribute_error(self):
        match = Match.objects.create(
            is_double=False,
            date_played=timezone.now()
        )

        self.assertFalse(match.user_can_edit(self.user1))

    def test_user_can_view_singles_match(self):
        """Test user_can_view delegation to edit permissions"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        
        self.assertTrue(match.user_can_view(self.user1))
        self.assertTrue(match.user_can_view(self.user2))
        
        other_user = User.objects.create_user(username="other", password="pass")
        self.assertFalse(match.user_can_view(other_user))
        
        staff = User.objects.create_user(username="staff", password="pass", is_staff=True)
        self.assertTrue(match.user_can_view(staff))
        
        # Verify delegation
        self.assertEqual(
            match.user_can_view(self.user1),
            match.user_can_edit(self.user1)
        )

    def test_user_can_view_doubles_match(self):
        """Test user_can_view delegation to edit permissions"""
        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2
        )

        self.assertTrue(match.user_can_view(self.user1))
        self.assertTrue(match.user_can_view(self.user2))
        self.assertTrue(match.user_can_view(self.user3))
        self.assertTrue(match.user_can_view(self.user4))

        other_user = User.objects.create_user(username="other", password="pass")
        self.assertFalse(match.user_can_view(other_user))

        staff = User.objects.create_user(username="staff", password="pass", is_staff=True)
        self.assertTrue(match.user_can_view(staff))

        # Verify delegation
        self.assertEqual(
            match.user_can_view(self.user1),
            match.user_can_edit(self.user1)
        )
    
    def test_user_cannot_edit_other_match(self):
        """Test user cannot edit matches they don't participate in"""
        other_user = User.objects.create_user(username="other", password="pass")
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        self.assertFalse(match.user_can_edit(other_user))
    
    def test_staff_can_edit_any_match(self):
        """Test staff can edit any match"""
        staff = User.objects.create_user(username="staff", password="pass", is_staff=True)
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        self.assertTrue(match.user_can_edit(staff))
    
    def test_unauthenticated_cannot_edit(self):
        """Test None/anonymous user cannot edit"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        self.assertFalse(match.user_can_edit(None))
    
    def test_should_not_confirm_already_confirmed_singles_match(self):
        """Test should_auto_confirm returns False for already confirmed match"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            winner=self.team1
        )

        match.confirmations.set([self.player1, self.player2])
        match.refresh_from_db()

        self.assertFalse(match.should_auto_confirm())

    def test_should_not_confirm_already_confirmed_doubles_match(self):
        """Test should_auto_confirm returns False for already confirmed match"""
        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2,
            winner=self.team_double1
        )

        match.confirmations.set([self.player1, self.player2, self.player3, self.player4])
        match.refresh_from_db()

        self.assertFalse(match.should_auto_confirm())
    
    def test_should_not_confirm_singles_match_without_winner(self):
        """Test should_auto_confirm returns False for match without winner"""
        match = Match.objects.create(
            team1=self.team1,
            team2=self.team2
        )
        self.assertFalse(match.should_auto_confirm())

    def test_should_not_confirm_doubles_match_without_winner(self):
        """Test should_auto_confirm returns False for match without winner"""
        match = Match.objects.create(
            team1=self.team_double1,
            team2=self.team_double2
        )
        self.assertFalse(match.should_auto_confirm())

class GameModelTest(TestCase):
    """Tests for the Game model"""
    
    def setUp(self):
        """Set up test data"""
        self.user1 = User.objects.create_user(username="p1", password="pass")
        self.user2 = User.objects.create_user(username="p2", password="pass")
        self.player1 = Player.objects.create(user=self.user1, name="P1")
        self.player2 = Player.objects.create(user=self.user2, name="P2")
        self.team1 = Team.objects.create()
        self.team1.players.set([self.player1])
        self.team2 = Team.objects.create()
        self.team2.players.set([self.player2])
        self.match = Match.objects.create(
            team1=self.team1,
            team2=self.team2,
            best_of=5
        )
    
    def test_game_creation(self):
        """Test creating a game"""
        game = Game.objects.create(
            match=self.match,
            game_number=1,
            team1_score=11,
            team2_score=5
        )
        self.assertEqual(game.match, self.match)
        self.assertEqual(game.game_number, 1)
        self.assertEqual(game.team1_score, 11)
        self.assertEqual(game.team2_score, 5)
    
    def test_game_auto_determines_winner(self):
        """Test game automatically determines winner"""
        game = Game.objects.create(
            match=self.match,
            game_number=1,
            team1_score=11,
            team2_score=5
        )
        self.assertEqual(game.winner, self.team1)
        
        game2 = Game.objects.create(
            match=self.match,
            game_number=2,
            team1_score=9,
            team2_score=11
        )
        self.assertEqual(game2.winner, self.team2)
    
    def test_game_str_representation(self):
        """Test game string representation"""
        game = Game.objects.create(
            match=self.match,
            game_number=1,
            team1_score=11,
            team2_score=9
        )
        self.assertEqual(str(game), "Game 1: 11-9")
    
    def test_game_unique_together_constraint(self):
        """Test game_number must be unique within a match"""
        Game.objects.create(
            match=self.match,
            game_number=1,
            team1_score=11,
            team2_score=5
        )
        
        # Creating another game with same number should fail
        with self.assertRaises(Exception):
            Game.objects.create(
                match=self.match,
                game_number=1,  # Duplicate
                team1_score=11,
                team2_score=9
            )
    
    def test_game_updates_match_winner(self):
        """Test that saving games updates the match winner"""
        # Create 3 games for player1 (best of 5)
        Game.objects.create(match=self.match, game_number=1, team1_score=11, team2_score=5)
        Game.objects.create(match=self.match, game_number=2, team1_score=11, team2_score=9)
        Game.objects.create(match=self.match, game_number=3, team1_score=11, team2_score=7)
        
        self.match.refresh_from_db()
        self.assertEqual(self.match.winner, self.team1)


class UserProfileModelTest(TestCase):
    """Tests for the UserProfile model"""
    
    def setUp(self):
        """Set up test data"""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
    
    def test_profile_created_with_signal(self):
        """Test profile is automatically created when user is created"""
        # Profile should be created by signal
        self.assertTrue(hasattr(self.user, 'profile'))
        self.assertIsInstance(self.user.profile, UserProfile)
    
    def test_profile_str_representation(self):
        """Test profile string representation"""
        self.assertEqual(str(self.user.profile), f"Profile of {self.user.username}")
    
    def test_create_verification_token(self):
        """Test creating email verification token"""
        token = self.user.profile.create_verification_token()
        
        self.assertIsNotNone(token)
        self.assertEqual(len(token), 32)
        self.assertEqual(self.user.profile.email_verification_token, token)
        self.assertIsNotNone(self.user.profile.email_verification_sent_at)
    
    def test_verify_email_with_correct_token(self):
        """Test email verification with correct token"""
        token = self.user.profile.create_verification_token()
        self.user.profile.save()
        
        result = self.user.profile.verify_email(token)
        
        self.assertTrue(result)
        self.assertTrue(self.user.profile.email_verified)
        self.assertEqual(self.user.profile.email_verification_token, "")
    
    def test_verify_email_with_incorrect_token(self):
        """Test email verification with incorrect token"""
        self.user.profile.create_verification_token()
        self.user.profile.save()
        
        result = self.user.profile.verify_email("wrong_token")
        
        self.assertFalse(result)
        self.assertFalse(self.user.profile.email_verified)
    
    def test_profile_defaults(self):
        """Test profile default values"""
        new_user = User.objects.create_user(username="new", password="pass")
        profile = new_user.profile
        
        self.assertFalse(profile.email_verified)
        self.assertIsNotNone(profile.email_verification_token)  # Set by signal
        self.assertIsNotNone(profile.created_at)

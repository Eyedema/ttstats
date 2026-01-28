import pytest
from datetime import date, time, datetime
from django.contrib.auth.models import AnonymousUser, User
from django.db import IntegrityError

from pingpong.models import Game, Location, Match, Player, ScheduledMatch, UserProfile, EloHistory
from .conftest import (
    GameFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
)


# ===========================================================================
# Location
# ===========================================================================

@pytest.mark.django_db
class TestLocation:
    def test_creation(self):
        loc = LocationFactory(name="Ping Pong Palace", address="123 Main St", notes="Great venue")
        assert loc.name == "Ping Pong Palace"
        assert loc.address == "123 Main St"
        assert loc.notes == "Great venue"
        assert loc.created_at is not None

    def test_str(self):
        loc = LocationFactory(name="The Arena")
        assert str(loc) == "The Arena"

    def test_ordering(self):
        LocationFactory(name="Zebra")
        LocationFactory(name="Alpha")
        LocationFactory(name="Middle")
        names = list(Location.objects.values_list("name", flat=True))
        assert names == ["Alpha", "Middle", "Zebra"]

    def test_blank_fields(self):
        loc = LocationFactory(name="Bare", address="", notes="")
        assert loc.address == ""
        assert loc.notes == ""


# ===========================================================================
# Player
# ===========================================================================

@pytest.mark.django_db
class TestPlayer:
    def test_creation_without_user(self):
        p = PlayerFactory(name="Guest")
        assert p.user is None
        assert p.name == "Guest"

    def test_creation_with_user(self):
        p = PlayerFactory(with_user=True, name="Linked")
        assert p.user is not None
        assert p.name == "Linked"

    def test_str_with_nickname(self):
        p = PlayerFactory(name="Full Name", nickname="Nick")
        assert str(p) == "Nick"

    def test_str_without_nickname(self):
        p = PlayerFactory(name="Full Name", nickname="")
        assert str(p) == "Full Name"

    def test_playing_style_choices(self):
        for style in ("normal", "hard_rubber", "unknown"):
            p = PlayerFactory(playing_style=style)
            assert p.playing_style == style

    def test_ordering(self):
        PlayerFactory(name="Zara")
        PlayerFactory(name="Alice")
        PlayerFactory(name="Bob")
        names = list(Player.objects.values_list("name", flat=True))
        assert names == ["Alice", "Bob", "Zara"]

    def test_user_can_edit_own(self):
        p = PlayerFactory(with_user=True)
        assert p.user_can_edit(p.user) is True

    def test_user_can_edit_staff(self):
        staff = UserFactory(is_staff=True)
        p = PlayerFactory(with_user=True)
        assert p.user_can_edit(staff) is True

    def test_user_can_edit_superuser(self):
        su = UserFactory(is_superuser=True)
        p = PlayerFactory(with_user=True)
        assert p.user_can_edit(su) is True

    def test_user_cannot_edit_other(self):
        other = UserFactory()
        p = PlayerFactory(with_user=True)
        assert p.user_can_edit(other) is False

    def test_user_can_edit_none(self):
        p = PlayerFactory(with_user=True)
        assert p.user_can_edit(None) is False

    def test_user_can_edit_anonymous(self):
        p = PlayerFactory(with_user=True)
        assert p.user_can_edit(AnonymousUser()) is False

    def test_user_can_edit_no_linked_user(self):
        """Player without linked user — only staff can edit."""
        p = PlayerFactory()  # no user
        regular = UserFactory()
        assert p.user_can_edit(regular) is False


# ===========================================================================
# Match
# ===========================================================================

@pytest.mark.django_db
class TestMatch:
    def test_creation(self):
        m = MatchFactory(match_type="tournament", best_of=7)
        assert m.player1 is not None
        assert m.player2 is not None
        assert m.match_type == "tournament"
        assert m.best_of == 7
        assert m.winner is None
        assert m.player1_confirmed is False
        assert m.player2_confirmed is False

    def test_str(self):
        m = MatchFactory()
        expected = f"{m.player1} vs {m.player2} - {m.date_played.date()}"
        assert str(m) == expected

    def test_player_scores_empty(self):
        m = MatchFactory()
        assert m.player1_score == 0
        assert m.player2_score == 0

    def test_player_scores_with_games(self):
        m = MatchFactory(best_of=5)
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=9, player2_score=11)
        m.refresh_from_db()
        assert m.player1_score == 1
        assert m.player2_score == 1

    def test_match_confirmed_property(self):
        m = MatchFactory()
        assert m.match_confirmed is False

        m.player1_confirmed = True
        assert m.match_confirmed is False

        m.player2_confirmed = True
        assert m.match_confirmed is True

    def test_should_auto_confirm_no_winner(self):
        m = MatchFactory()
        assert m.should_auto_confirm() is False

    def test_should_auto_confirm_already_confirmed(self):
        m = MatchFactory(player1_confirmed=True, player2_confirmed=True)
        # Need to set winner manually without triggering save auto-logic
        Match.objects.filter(pk=m.pk).update(winner=m.player1)
        m.refresh_from_db()
        assert m.should_auto_confirm() is False

    def test_should_auto_confirm_unverified_player(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = False
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = False
        m.player2.user.profile.save()
        Match.objects.filter(pk=m.pk).update(winner=m.player1)
        m.refresh_from_db()
        assert m.should_auto_confirm() is True

    def test_should_auto_confirm_both_verified(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = True
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = True
        m.player2.user.profile.save()
        Match.objects.filter(pk=m.pk).update(winner=m.player1)
        m.refresh_from_db()
        assert m.should_auto_confirm() is False

    def test_should_auto_confirm_no_user(self):
        """Player without a user account -> unverified -> auto-confirm."""
        p1 = PlayerFactory()  # no user
        p2 = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p1, player2=p2)
        Match.objects.filter(pk=m.pk).update(winner=p1)
        m.refresh_from_db()
        assert m.should_auto_confirm() is True

    def test_get_unverified_players_both(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = False
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = False
        m.player2.user.profile.save()
        unverified = m.get_unverified_players()
        assert len(unverified) == 2

    def test_get_unverified_players_one(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = True
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = False
        m.player2.user.profile.save()
        unverified = m.get_unverified_players()
        assert len(unverified) == 1
        assert m.player2 in unverified

    def test_get_unverified_players_none(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = True
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = True
        m.player2.user.profile.save()
        assert m.get_unverified_players() == []

    def test_user_can_edit_participant(self):
        m = MatchFactory()
        assert m.user_can_edit(m.player1.user) is True
        assert m.user_can_edit(m.player2.user) is True

    def test_user_can_edit_staff(self):
        staff = UserFactory(is_staff=True)
        m = MatchFactory()
        assert m.user_can_edit(staff) is True

    def test_user_can_edit_superuser(self):
        su = UserFactory(is_superuser=True)
        m = MatchFactory()
        assert m.user_can_edit(su) is True

    def test_user_cannot_edit_non_participant(self):
        other = UserFactory()
        m = MatchFactory()
        assert m.user_can_edit(other) is False

    def test_user_can_edit_none(self):
        m = MatchFactory()
        assert m.user_can_edit(None) is False

    def test_user_can_edit_anonymous(self):
        m = MatchFactory()
        assert m.user_can_edit(AnonymousUser()) is False

    def test_user_can_view_delegates_to_edit(self):
        m = MatchFactory()
        user = m.player1.user
        assert m.user_can_view(user) == m.user_can_edit(user)

    def test_auto_winner_best_of_5_player1(self):
        m = MatchFactory(best_of=5)
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
        GameFactory(match=m, game_number=3, player1_score=11, player2_score=7)
        m.refresh_from_db()
        assert m.winner == m.player1

    def test_auto_winner_best_of_5_player2(self):
        m = MatchFactory(best_of=5)
        GameFactory(match=m, game_number=1, player1_score=5, player2_score=11)
        GameFactory(match=m, game_number=2, player1_score=9, player2_score=11)
        GameFactory(match=m, game_number=3, player1_score=7, player2_score=11)
        m.refresh_from_db()
        assert m.winner == m.player2

    def test_auto_winner_best_of_3(self):
        m = MatchFactory(best_of=3)
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
        m.refresh_from_db()
        assert m.winner == m.player1

    def test_auto_winner_best_of_7(self):
        m = MatchFactory(best_of=7)
        for i in range(1, 5):
            GameFactory(match=m, game_number=i, player1_score=11, player2_score=5)
        m.refresh_from_db()
        assert m.winner == m.player1

    def test_no_winner_on_new_match(self):
        m = MatchFactory()
        assert m.winner is None

    def test_no_winner_insufficient_games(self):
        m = MatchFactory(best_of=5)
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
        # Only 2 wins, need 3 for best of 5
        m.refresh_from_db()
        assert m.winner is None

    def test_ordering(self):
        from django.utils import timezone
        from datetime import timedelta

        m1 = MatchFactory()
        Match.objects.filter(pk=m1.pk).update(date_played=timezone.now() - timedelta(days=2))
        m2 = MatchFactory()
        Match.objects.filter(pk=m2.pk).update(date_played=timezone.now() - timedelta(days=1))

        # Ordering is -date_played (newest first)
        pks = list(Match.objects.values_list("pk", flat=True))
        assert pks[0] == m2.pk

    def test_location_set_null_on_delete(self):
        loc = LocationFactory()
        m = MatchFactory(location=loc)
        loc.delete()
        m.refresh_from_db()
        assert m.location is None


# ===========================================================================
# Game
# ===========================================================================

@pytest.mark.django_db
class TestGame:
    def test_creation(self):
        m = MatchFactory()
        g = GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        assert g.match == m
        assert g.game_number == 1
        assert g.player1_score == 11
        assert g.player2_score == 5

    def test_str(self):
        g = GameFactory(game_number=3, player1_score=11, player2_score=9)
        assert str(g) == "Game 3: 11-9"

    def test_auto_winner_player1(self):
        m = MatchFactory()
        g = GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        assert g.winner == m.player1

    def test_auto_winner_player2(self):
        m = MatchFactory()
        g = GameFactory(match=m, game_number=1, player1_score=5, player2_score=11)
        assert g.winner == m.player2

    def test_duce_score_no_winner(self):
        m = MatchFactory()
        g = Game(match=m, game_number=1, player1_score=11, player2_score=11)
        g.save()
        assert g.winner is None

    def test_duce_score_with_winner(self):
        m = MatchFactory()
        g = Game(match=m, game_number=1, player1_score=13, player2_score=11)
        g.save()
        assert g.winner == m.player1

    def test_unique_together_constraint(self):
        m = MatchFactory()
        GameFactory(match=m, game_number=1)
        with pytest.raises(IntegrityError):
            GameFactory(match=m, game_number=1)

    def test_game_save_triggers_match_save(self):
        m = MatchFactory(best_of=3)
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
        m.refresh_from_db()
        assert m.winner == m.player1

    def test_ordering(self):
        m = MatchFactory()
        GameFactory(match=m, game_number=3, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=5)
        numbers = list(m.games.values_list("game_number", flat=True))
        assert numbers == [1, 2, 3]

    def test_duration_minutes_optional(self):
        m = MatchFactory()
        g = GameFactory(match=m, game_number=1, duration_minutes=None)
        assert g.duration_minutes is None
        g2 = GameFactory(match=m, game_number=2, duration_minutes=15)
        assert g2.duration_minutes == 15


# ===========================================================================
# UserProfile
# ===========================================================================

@pytest.mark.django_db
class TestUserProfile:
    def test_auto_creation_via_signal(self):
        u = UserFactory()
        assert hasattr(u, "profile")
        assert isinstance(u.profile, UserProfile)

    def test_str(self):
        u = UserFactory(username="mario")
        assert str(u.profile) == "Profile of mario"

    def test_create_verification_token(self):
        u = UserFactory()
        token = u.profile.create_verification_token()
        assert len(token) == 32
        assert u.profile.email_verification_token == token
        assert u.profile.email_verification_sent_at is not None

    def test_verify_email_correct_token(self):
        u = UserFactory()
        token = u.profile.create_verification_token()
        u.profile.save()
        result = u.profile.verify_email(token)
        assert result is True
        assert u.profile.email_verified is True
        assert u.profile.email_verification_token == ""

    def test_verify_email_incorrect_token(self):
        u = UserFactory()
        u.profile.create_verification_token()
        u.profile.save()
        result = u.profile.verify_email("wrong_token")
        assert result is False
        assert u.profile.email_verified is False

    def test_verify_email_empty_token(self):
        u = UserFactory()
        # Token is set by signal, so clear it
        u.profile.email_verification_token = ""
        u.profile.save()
        result = u.profile.verify_email("")
        # Empty string matches empty string
        assert result is True

    def test_defaults(self):
        u = UserFactory()
        assert u.profile.email_verified is False
        assert u.profile.email_verification_token != ""
        assert u.profile.created_at is not None


# ===========================================================================
# ScheduledMatch
# ===========================================================================

@pytest.mark.django_db
class TestScheduledMatch:
    def test_creation(self):
        sm = ScheduledMatchFactory()
        assert sm.player1 is not None
        assert sm.player2 is not None
        assert sm.scheduled_date is not None
        assert sm.scheduled_time is not None

    def test_str(self):
        sm = ScheduledMatchFactory()
        expected = f"{sm.player1} vs {sm.player2} - {sm.scheduled_date} at {sm.scheduled_time}"
        assert str(sm) == expected

    def test_scheduled_datetime_property(self):
        sm = ScheduledMatchFactory(
            scheduled_date=date(2025, 6, 15),
            scheduled_time=time(14, 30),
        )
        dt = sm.scheduled_datetime
        assert isinstance(dt, datetime)
        assert dt == datetime(2025, 6, 15, 14, 30)

    def test_notification_sent_default(self):
        sm = ScheduledMatchFactory()
        assert sm.notification_sent is False

    def test_ordering(self):
        from datetime import timedelta

        sm1 = ScheduledMatchFactory(
            scheduled_date=date.today() + timedelta(days=10),
            scheduled_time=time(10, 0),
        )
        sm2 = ScheduledMatchFactory(
            scheduled_date=date.today() + timedelta(days=5),
            scheduled_time=time(10, 0),
        )
        pks = list(ScheduledMatch.objects.values_list("pk", flat=True))
        assert pks[0] == sm2.pk

    def test_user_can_view_participant(self):
        sm = ScheduledMatchFactory()
        assert sm.user_can_view(sm.player1.user) is True
        assert sm.user_can_view(sm.player2.user) is True

    def test_user_can_view_staff(self):
        staff = UserFactory(is_staff=True)
        sm = ScheduledMatchFactory()
        assert sm.user_can_view(staff) is True

    def test_user_can_view_non_participant(self):
        other = UserFactory()
        sm = ScheduledMatchFactory()
        assert sm.user_can_view(other) is False

    def test_user_can_view_none(self):
        sm = ScheduledMatchFactory()
        assert sm.user_can_view(None) is False

    def test_user_can_view_anonymous(self):
        sm = ScheduledMatchFactory()
        assert sm.user_can_view(AnonymousUser()) is False

    def test_user_can_edit_delegates_to_view(self):
        sm = ScheduledMatchFactory()
        user = sm.player1.user
        assert sm.user_can_edit(user) == sm.user_can_view(user)

    def test_user_can_view_player_without_user(self):
        p1 = PlayerFactory()  # no user
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)
        other = UserFactory()
        assert sm.user_can_view(other) is False


@pytest.mark.django_db
class TestPlayerEloFields:
    """Test Player Elo rating fields"""

    def test_default_elo_rating(self):
        """New players should start with Elo 1500"""
        player = PlayerFactory()
        assert player.elo_rating == 1500

    def test_default_elo_peak(self):
        """New players should have peak Elo of 1500"""
        player = PlayerFactory()
        assert player.elo_peak == 1500

    def test_default_matches_for_elo(self):
        """New players should have 0 matches for Elo"""
        player = PlayerFactory()
        assert player.matches_for_elo == 0

    def test_custom_elo_rating(self):
        """Player can be created with custom Elo"""
        player = PlayerFactory(elo_rating=1650, elo_peak=1700, matches_for_elo=50)
        assert player.elo_rating == 1650
        assert player.elo_peak == 1700
        assert player.matches_for_elo == 50


@pytest.mark.django_db
class TestEloHistory:
    """Test EloHistory model"""

    def test_create_elo_history(self):
        """EloHistory can be created with all required fields"""
        match = MatchFactory()
        player = match.player1

        history = EloHistory.objects.create(
            match=match,
            player=player,
            old_rating=1500,
            new_rating=1516,
            rating_change=16,
            k_factor=32.0,
        )

        assert history.match == match
        assert history.player == player
        assert history.old_rating == 1500
        assert history.new_rating == 1516
        assert history.rating_change == 16
        assert history.k_factor == 32.0

    def test_elo_history_str(self):
        """EloHistory __str__ should show player, change, and match"""
        match = MatchFactory()
        player = match.player1

        history = EloHistory.objects.create(
            match=match,
            player=player,
            old_rating=1500,
            new_rating=1516,
            rating_change=16,
            k_factor=32.0,
        )

        assert str(history) == f"{player} +16 ({match})"

    def test_elo_history_negative_change(self):
        """EloHistory __str__ should handle negative changes"""
        match = MatchFactory()
        player = match.player2

        history = EloHistory.objects.create(
            match=match,
            player=player,
            old_rating=1500,
            new_rating=1484,
            rating_change=-16,
            k_factor=32.0,
        )

        assert str(history) == f"{player} -16 ({match})"

    def test_unique_together_constraint(self):
        """Cannot create duplicate EloHistory for same match + player"""
        from django.db import IntegrityError
        
        match = MatchFactory()
        player = match.player1

        EloHistory.objects.create(
            match=match,
            player=player,
            old_rating=1500,
            new_rating=1516,
            rating_change=16,
            k_factor=32.0,
        )

        with pytest.raises(IntegrityError):
            EloHistory.objects.create(
                match=match,
                player=player,
                old_rating=1500,
                new_rating=1520,
                rating_change=20,
                k_factor=32.0,
            )

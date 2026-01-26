import pytest
from datetime import date, timedelta, time
from django.utils import timezone

from pingpong.forms import GameForm, MatchEditForm, MatchForm, PlayerRegistrationForm, ScheduledMatchForm
from pingpong.models import Player, User
from .conftest import LocationFactory, MatchFactory, PlayerFactory, UserFactory


# ===========================================================================
# MatchForm
# ===========================================================================

@pytest.mark.django_db
class TestMatchForm:
    def test_valid_data(self):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        form = MatchForm(data={
            "player1": p1.pk,
            "player2": p2.pk,
            "date_played": timezone.now(),
            "match_type": "casual",
            "best_of": 5,
        })
        assert form.is_valid(), form.errors

    def test_same_player_validation(self):
        p = PlayerFactory()
        form = MatchForm(data={
            "player1": p.pk,
            "player2": p.pk,
            "date_played": timezone.now(),
            "match_type": "casual",
            "best_of": 5,
        })
        assert not form.is_valid()
        assert "__all__" in form.errors

    def test_missing_required_fields(self):
        form = MatchForm(data={})
        assert not form.is_valid()
        assert "player1" in form.errors
        assert "player2" in form.errors


# ===========================================================================
# MatchEditForm
# ===========================================================================

@pytest.mark.django_db
class TestMatchEditForm:
    def test_valid_data(self):
        loc = LocationFactory()
        form = MatchEditForm(data={
            "location": loc.pk,
            "notes": "Good match",
        })
        assert form.is_valid(), form.errors

    def test_limited_fields(self):
        assert list(MatchEditForm.Meta.fields) == ["location", "notes"]


# ===========================================================================
# GameForm
# ===========================================================================

@pytest.mark.django_db
class TestGameForm:
    def test_valid_data(self):
        form = GameForm(data={
            "game_number": 1,
            "player1_score": 11,
            "player2_score": 5,
        })
        assert form.is_valid(), form.errors

    def test_tie_score_rejected(self):
        form = GameForm(data={
            "game_number": 1,
            "player1_score": 10,
            "player2_score": 10,
        })
        assert not form.is_valid()
        assert "A game cannot end in a tie!" in str(form.errors)

    def test_deuce_must_win_by_2(self):
        form = GameForm(data={
            "game_number": 1,
            "player1_score": 11,
            "player2_score": 10,
        })
        assert not form.is_valid()
        assert "must win by 2" in str(form.errors)

    def test_valid_deuce_score(self):
        form = GameForm(data={
            "game_number": 1,
            "player1_score": 12,
            "player2_score": 10,
        })
        assert form.is_valid(), form.errors

    def test_edge_scores(self):
        form = GameForm(data={
            "game_number": 1,
            "player1_score": 15,
            "player2_score": 13,
        })
        assert form.is_valid(), form.errors

    def test_normal_score_no_deuce_rule(self):
        """Score below 10-10 doesn't need win-by-2."""
        form = GameForm(data={
            "game_number": 1,
            "player1_score": 11,
            "player2_score": 9,
        })
        assert form.is_valid(), form.errors

    def test_duration_minutes_optional(self):
        form = GameForm(data={
            "game_number": 1,
            "player1_score": 11,
            "player2_score": 5,
        })
        assert form.is_valid()

        form2 = GameForm(data={
            "game_number": 1,
            "player1_score": 11,
            "player2_score": 5,
            "duration_minutes": 10,
        })
        assert form2.is_valid()


# ===========================================================================
# PlayerRegistrationForm
# ===========================================================================

@pytest.mark.django_db
class TestPlayerRegistrationForm:
    def test_valid_registration(self):
        form = PlayerRegistrationForm(data={
            "username": "newplayer",
            "email": "new@example.com",
            "password1": "Str0ngP@ssw0rd!",
            "password2": "Str0ngP@ssw0rd!",
            "full_name": "New Player",
            "nickname": "Newbie",
            "playing_style": "normal",
        })
        assert form.is_valid(), form.errors
        user = form.save()
        assert user.username == "newplayer"
        assert user.email == "new@example.com"
        # Player profile should be created
        player = Player.objects.get(user=user)
        assert player.name == "New Player"
        assert player.nickname == "Newbie"
        assert player.playing_style == "normal"

    def test_duplicate_username(self):
        UserFactory(username="existing")
        form = PlayerRegistrationForm(data={
            "username": "existing",
            "email": "new@example.com",
            "password1": "Str0ngP@ssw0rd!",
            "password2": "Str0ngP@ssw0rd!",
            "full_name": "Player",
            "playing_style": "normal",
        })
        assert not form.is_valid()
        assert "username" in form.errors

    def test_password_mismatch(self):
        form = PlayerRegistrationForm(data={
            "username": "testuser",
            "email": "test@example.com",
            "password1": "Str0ngP@ssw0rd!",
            "password2": "DifferentPass!",
            "full_name": "Player",
            "playing_style": "normal",
        })
        assert not form.is_valid()
        assert "password2" in form.errors

    def test_save_commit_false(self):
        form = PlayerRegistrationForm(data={
            "username": "nocommit",
            "email": "nc@example.com",
            "password1": "Str0ngP@ssw0rd!",
            "password2": "Str0ngP@ssw0rd!",
            "full_name": "No Commit",
            "playing_style": "normal",
        })
        assert form.is_valid()
        user = form.save(commit=False)
        # User not saved yet
        assert user.pk is None
        assert not Player.objects.filter(name="No Commit").exists()


# ===========================================================================
# ScheduledMatchForm
# ===========================================================================

@pytest.mark.django_db
class TestScheduledMatchForm:
    def test_valid_data(self):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        form = ScheduledMatchForm(data={
            "player1": p1.pk,
            "player2": p2.pk,
            "scheduled_date": date.today() + timedelta(days=3),
            "scheduled_time": "14:00",
        })
        assert form.is_valid(), form.errors

    def test_same_player_rejected(self):
        p = PlayerFactory()
        form = ScheduledMatchForm(data={
            "player1": p.pk,
            "player2": p.pk,
            "scheduled_date": date.today() + timedelta(days=3),
            "scheduled_time": "14:00",
        })
        assert not form.is_valid()

    def test_past_date_rejected(self):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        form = ScheduledMatchForm(data={
            "player1": p1.pk,
            "player2": p2.pk,
            "scheduled_date": date.today() - timedelta(days=1),
            "scheduled_time": "14:00",
        })
        assert not form.is_valid()

    def test_today_accepted(self):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        form = ScheduledMatchForm(data={
            "player1": p1.pk,
            "player2": p2.pk,
            "scheduled_date": date.today(),
            "scheduled_time": "14:00",
        })
        assert form.is_valid(), form.errors

    def test_future_date_accepted(self):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        form = ScheduledMatchForm(data={
            "player1": p1.pk,
            "player2": p2.pk,
            "scheduled_date": date.today() + timedelta(days=30),
            "scheduled_time": "14:00",
        })
        assert form.is_valid(), form.errors

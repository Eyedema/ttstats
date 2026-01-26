# Create your models here.
import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .managers import GameManager, MatchManager, PlayerManager, ScheduledMatchManager


class Location(models.Model):
    """Location where matches are played"""

    name = models.CharField(max_length=100)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Player(models.Model):
    """General player model"""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Link to Django user if they have an account",
    )
    name = models.CharField(max_length=100)
    nickname = models.CharField(max_length=50, blank=True)
    playing_style = models.CharField(
        max_length=50,
        choices=[
            ("normal", "Normal"),
            ("hard_rubber", "Hard rubber"),
            ("unknown", "Unknown"),
        ],
        default="normal",
    )
    notes = models.TextField(blank=True, help_text="Strengths, weaknesses, etc.")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PlayerManager()

    def user_can_edit(self, user):
        """Check if given user can edit this player"""
        if not user or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        if self.user and self.user == user:
            return True
        return False

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.nickname if self.nickname else self.name


class Match(models.Model):
    """Individual match between two players"""

    player1 = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="matches_as_player1"
    )
    player2 = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="matches_as_player2"
    )
    date_played = models.DateTimeField(default=timezone.now)
    location = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, blank=True
    )

    match_type = models.CharField(
        max_length=20,
        choices=[
            ("casual", "Casual"),
            ("practice", "Practice"),
            ("tournament", "Tournament"),
        ],
        default="casual",
    )

    # Best of format (best of 3, 5, 7, etc.)
    best_of = models.IntegerField(default=5, help_text="Best of how many games?")

    winner = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    player1_confirmed = models.BooleanField(default=False)
    player2_confirmed = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = MatchManager()

    def user_can_edit(self, user):
        """Check if user can edit this match"""
        if not user or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        return self.player1.user == user or self.player2.user == user

    def user_can_view(self, user):
        """Check if user can view this match"""
        # Same as edit for now (could be different)
        return self.user_can_edit(user)

    class Meta:
        ordering = ["-date_played"]
        verbose_name_plural = "matches"

    def __str__(self):
        return f"{self.player1} vs {self.player2} - {self.date_played.date()}"

    @property
    def player1_score(self):
        return self.games.filter(winner=self.player1).count()  # type: ignore

    @property
    def player2_score(self):
        return self.games.filter(winner=self.player2).count()  # type: ignore

    @property
    def match_confirmed(self):
        return self.player1_confirmed & self.player2_confirmed

    def should_auto_confirm(self):
        if not self.winner:
            return False
        if self.match_confirmed:
            return False
        for player in [self.player1, self.player2]:
            if not player.user or not player.user.profile.email_verified:
                return True
        return False

    def get_unverified_players(self):
        unverified = []
        for player in [self.player1, self.player2]:
            if not player.user or not player.user.profile.email_verified:
                unverified.append(player)
        return unverified

    def save(self, *args, **kwargs):
        # Auto-determine winner based on games
        if self.pk:  # Only if match already exists
            p1_wins = self.games.filter(winner=self.player1).count()  # type: ignore
            p2_wins = self.games.filter(winner=self.player2).count()  # type: ignore
            games_to_win = (self.best_of // 2) + 1

            if p1_wins >= games_to_win:
                self.winner = self.player1
            elif p2_wins >= games_to_win:
                self.winner = self.player2
        super().save(*args, **kwargs)


class Game(models.Model):
    """Individual game within a match"""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="games")
    game_number = models.IntegerField(help_text="1st game, 2nd game, etc.")
    player1_score = models.IntegerField(default=0)
    player2_score = models.IntegerField(default=0)

    winner = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="won_games",
    )

    duration_minutes = models.IntegerField(null=True, blank=True)

    objects = GameManager()

    class Meta:
        ordering = ["game_number"]
        unique_together = ["match", "game_number"]

    def __str__(self):
        return f"Game {self.game_number}: {self.player1_score}-{self.player2_score}"

    def save(self, *args, **kwargs):
        # Auto-determine winner
        if self.player1_score > self.player2_score:
            self.winner = self.match.player1
        elif self.player2_score > self.player1_score:
            self.winner = self.match.player2

        super().save(*args, **kwargs)

        # Update match winner
        self.match.save()


class UserProfile(models.Model):
    """Extended user profile for additional information"""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    email_verified = models.BooleanField(default=False)
    email_verification_token = models.CharField(max_length=64, blank=True)
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def create_verification_token(self):
        self.email_verification_token = uuid.uuid4().hex
        self.email_verification_sent_at = timezone.now()
        return self.email_verification_token

    def verify_email(self, token):
        if self.email_verification_token == token:
            self.email_verified = True
            self.email_verification_token = ""
            self.save()
            return True
        return False

    def __str__(self):
        return f"Profile of {self.user.username}"


class ScheduledMatch(models.Model):
    """A match scheduled for the future"""

    player1 = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="scheduled_matches_as_player1"
    )
    player2 = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="scheduled_matches_as_player2"
    )
    scheduled_date = models.DateField(help_text="Date of the scheduled match")
    scheduled_time = models.TimeField(help_text="Time of the scheduled match")
    location = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, blank=True
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_matches_created",
    )

    # Track if emails were sent
    notification_sent = models.BooleanField(default=False)

    objects = ScheduledMatchManager()

    class Meta:
        ordering = ["scheduled_date", "scheduled_time"]
        verbose_name = "Scheduled Match"
        verbose_name_plural = "Scheduled Matches"

    def __str__(self):
        return f"{self.player1} vs {self.player2} - {self.scheduled_date} at {self.scheduled_time}"

    @property
    def scheduled_datetime(self):
        """Combine date and time into a datetime object"""
        from datetime import datetime
        return datetime.combine(self.scheduled_date, self.scheduled_time)

    def user_can_view(self, user):
        """Check if user can view this scheduled match"""
        if not user or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        try:
            return self.player1.user == user or self.player2.user == user
        except AttributeError:
            return False

    def user_can_edit(self, user):
        """Check if user can edit this scheduled match"""
        return self.user_can_view(user)

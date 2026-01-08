# Create your models here.
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


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
    location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True)

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

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
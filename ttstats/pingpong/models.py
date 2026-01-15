# Create your models here.
import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .managers import GameManager, MatchManager, PlayerManager


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


class Team(models.Model):
    """Concept used for matches to include both singles and doubles score"""

    players = models.ManyToManyField(Player, related_name="teams")
    name = models.CharField(max_length=100, blank=True)

    def __str__(self):
        if self.name:
            return self.name

        # Default: "Player1 and Player2"
        players_list = self.players.order_by('name').all()
        if len(players_list) == 1:
            return str(players_list[0])
        elif len(players_list) == 2:
            return f"{players_list[0]} and {players_list[1]}"
        else:
            names = [p.name for p in players_list[:2]]
            return f"{names[0]} and {names[1]} (+{len(players_list) - 2})"


class Match(models.Model):
    """Individual match between two players"""

    is_double = models.BooleanField(default=False)

    team1 = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="matches_as_team11"
    )
    team2 = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="matches_as_team2"
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

    winners = models.ManyToManyField(
        "Player",
        related_name="matches_won",
        blank=True,
    )

    confirmations = models.ManyToManyField(Player, through='MatchConfirmation')
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
        try:
            return user in self.team1.players or user in self.team2.players
        except AttributeError:
            return False

    def user_can_view(self, user):
        """Check if user can view this match"""
        # Same as edit for now (could be different)
        return self.user_can_edit(user)

    class Meta:
        ordering = ["-date_played"]
        verbose_name_plural = "matches"

    def __str__(self):
        return f"{self.team1} vs {self.team2} - {self.date_played.date()}"

    @property
    def team1_score(self):
        return self.games.filter(winner__in=self.team1.players.all()).count()

    @property
    def team2_score(self):
        return self.games.filter(winner__in=self.team2.players.all()).count()

    @property
    def team1_confirmed(self):
        """All Team 1 members have confirmed"""
        team1_players = self.team1.players.all()
        if not team1_players.exists():
            return False
        confirmed_players = self.confirmations.all()
        return team1_players.count() == confirmed_players.count()

    @property
    def team2_confirmed(self):
        """All Team 2 members have confirmed"""
        team2_players = self.team2.players.all()
        if not team2_players.exists():
            return False
        confirmed_players = self.confirmations.all()
        return team2_players.count() == confirmed_players.count()

    @property
    def is_fully_confirmed(self):
        """Tutti i giocatori di entrambi i team hanno confermato"""
        return self.team1_confirmed and self.team2_confirmed

    def should_auto_confirm(self):
        if not self.winners or self.is_fully_confirmed:
            return False
        for player in [self.team1.players, self.team2.players]:
            if not player.user or not player.user.profile.email_verified:
                return True
        return False

    def get_unverified_players(self):
        unverified = []
        for player in [self.team1.players, self.team2.players]:
            if not player.user or not player.user.profile.email_verified:
                unverified.append(player)
        return unverified

    def save(self, *args, **kwargs):
        # Auto-determine winner based on games
        if self.pk:  # Only if match already exists
            p1_wins = self.games.filter(winners=self.team1.players).count()  # type: ignore
            p2_wins = self.games.filter(winners=self.team2.players).count()  # type: ignore
            games_to_win = (self.best_of // 2) + 1

            if p1_wins >= games_to_win:
                self.winner = self.team1
            elif p2_wins >= games_to_win:
                self.winner = self.team2
        super().save(*args, **kwargs)


class MatchConfirmation(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='confirmations')
    player = models.ForeignKey(Player, on_delete=models.CASCADE)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('match', 'player')  # Players need to confirm only once
        verbose_name_plural = "Match Confirmations"

    def __str__(self):
        return f"{self.player} confirmed {self.match}"


class Game(models.Model):
    """Individual game within a match"""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="games")
    game_number = models.IntegerField(help_text="1st game, 2nd game, etc.")
    team1_score = models.IntegerField(default=0)
    team2_score = models.IntegerField(default=0)

    winners = models.ManyToManyField(
        "Player",
        related_name="games_won",
        blank=True,
    )

    duration_minutes = models.IntegerField(null=True, blank=True)

    objects = GameManager()

    class Meta:
        ordering = ["game_number"]
        unique_together = ["match", "game_number"]

    def __str__(self):
        return f"Game {self.game_number}: {self.team1_score}-{self.team2_score}"

    def save(self, *args, **kwargs):
        # Auto-determine winner
        if self.team1_score > self.team2_score:
            self.winners = self.match.team1
        elif self.team2_score > self.team1_score:
            self.winners = self.match.team2

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

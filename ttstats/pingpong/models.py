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

    @property
    def win_rate(self):
        matches = self.matches_won.count()
        total = self.matches_played.count()
        return round(matches / total * 100, 1) if total else 0

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
        Team, on_delete=models.CASCADE, related_name="matches_as_team1", null=True
    )
    team2 = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="matches_as_team2", null=True
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
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    confirmations = models.ManyToManyField(Player, through='MatchConfirmation', related_name="player_matchconfirmations")
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
            return (self.team1.players.filter(user_id=user.pk).exists() or
                    self.team2.players.filter(user_id=user.pk).exists())
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
        return self.games.filter(winner=self.team1).count()

    @property
    def team2_score(self):
        return self.games.filter(winner=self.team2).count()

    @property
    def team1_confirmed(self):
        """All Team 1 members have confirmed"""
        team1_players = self.team1.players.all()
        team1_ids = {p.id for p in team1_players}
        confirmed_ids = {c.id for c in self.confirmations.all()}

        if team1_ids.issubset(confirmed_ids):
            return True

        all_unverified = all(
            not (p.user and p.user.profile.email_verified)
            for p in team1_players
        )

        return all_unverified

    @property
    def team2_confirmed(self):
        """All Team 2 members have confirmed"""
        team2_players = self.team2.players.all()
        team2_ids = {p.id for p in team2_players}
        confirmed_ids = {c.id for c in self.confirmations.all()}

        if team2_ids.issubset(confirmed_ids):
            return True

        all_unverified = all(
            not (p.user and p.user.profile.email_verified)
            for p in team2_players
        )

        return all_unverified

    @property
    def match_confirmed(self):
        """Tutti i giocatori di entrambi i team hanno confermato"""
        return self.team1_confirmed and self.team2_confirmed

    def should_auto_confirm(self):
        if not self.winner or self.match_confirmed:
            return False

        team1_all_unverified = True
        for player in self.team1.players.all():
            if player.user and player.user.profile.email_verified:
                team1_all_unverified = False
                break

        team2_all_unverified = True
        for player in self.team2.players.all():
            if player.user and player.user.profile.email_verified:
                team2_all_unverified = False
                break

        return team1_all_unverified or team2_all_unverified

    def get_unverified_players(self):
        unverified = []

        all_players = (self.team1.players.all() | self.team2.players.all())

        for player in all_players:
            if not player.user or not player.user.profile.email_verified:
                unverified.append(player)

        return unverified

    def save(self, *args, **kwargs):
        # Auto-determine winner based on games
        if self.pk:  # Only if match already exists
            t1_wins = self.games.filter(winner=self.team1).count()  # type: ignore
            t2_wins = self.games.filter(winner=self.team2).count()  # type: ignore
            games_to_win = (self.best_of // 2) + 1

            if t1_wins >= games_to_win:
                self.winner = self.team1
            elif t2_wins >= games_to_win:
                self.winner = self.team2
        super().save(*args, **kwargs)


class MatchConfirmation(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE)
    player = models.ForeignKey(Player, on_delete=models.CASCADE)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('match', 'player')  # Players need to confirm only once


class Game(models.Model):
    """Individual game within a match"""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="games")
    game_number = models.IntegerField(help_text="1st game, 2nd game, etc.")
    team1_score = models.IntegerField(default=0)
    team2_score = models.IntegerField(default=0)

    winner = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="games_won", # TODO: before it was won_games, search and replace it!
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
            self.winner = self.match.team1
        elif self.team2_score > self.team1_score:
            self.winner = self.match.team2

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

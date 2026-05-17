# Create your models here.
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .managers import ChampionshipManager, GameManager, LiveMatchManager, MatchManager, PlayerManager, ScheduledMatchManager

# Email verification token expires after 24 hours
VERIFICATION_TOKEN_EXPIRY = timedelta(hours=24)


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

    # Elo rating fields
    elo_rating = models.IntegerField(
        default=1500,
        help_text="Current Elo rating"
    )
    elo_peak = models.IntegerField(
        default=1500,
        help_text="All-time highest Elo rating"
    )
    matches_for_elo = models.IntegerField(
        default=0,
        help_text="Number of confirmed matches that affected Elo (for new player boost)"
    )

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
    championship = models.ForeignKey(
        'Championship',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='matches',
        help_text="Championship this match belongs to (if any)"
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

    # Denormalized cache fields for performance
    is_confirmed = models.BooleanField(default=False, db_index=True)
    team1_score_cache = models.IntegerField(default=0)
    team2_score_cache = models.IntegerField(default=0)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Live scoreboard mode (in-match scoring)
    is_live = models.BooleanField(default=False, db_index=True)
    live_state = models.JSONField(null=True, blank=True)
    scorekeeper = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="live_matches",
        help_text="Player driving the live scoreboard for this match",
    )

    all_objects = models.Manager()
    objects = MatchManager()
    live_objects = LiveMatchManager()

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
        default_manager_name = 'objects'
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
        team1_players = self.team1.players.filter(user__profile__email_verified=True)
        team1_ids = {p.id for p in team1_players}
        confirmed_ids = {c.id for c in self.confirmations.all()}

        if team1_ids.issubset(confirmed_ids):
            return True

        all_unverified = all(
            not (p.user and p.user.profile.email_verified)
            for p in team1_players.all()
        )

        return all_unverified

    @property
    def team2_confirmed(self):
        """All Team 2 members have confirmed"""
        team2_players = self.team2.players.filter(user__profile__email_verified=True)
        team2_ids = {p.id for p in team2_players}
        confirmed_ids = {c.id for c in self.confirmations.all()}

        if team2_ids.issubset(confirmed_ids):
            return True

        all_unverified = all(
            not (p.user and p.user.profile.email_verified)
            for p in team2_players.all()
        )

        return all_unverified

    @property
    def match_confirmed(self):
        """Tutti i giocatori di entrambi i team hanno confermato"""
        return self.team1_confirmed and self.team2_confirmed

    @property
    def player1(self):
        """Backward-compatible property: returns first player from team1"""
        if self.team1:
            return self.team1.players.first()
        return None

    @property
    def player2(self):
        """Backward-compatible property: returns first player from team2"""
        if self.team2:
            return self.team2.players.first()
        return None

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

    def update_cache_fields(self):
        """Update all denormalized cache fields. Call from signals after changes."""
        self.team1_score_cache = self.games.filter(winner=self.team1).count()
        self.team2_score_cache = self.games.filter(winner=self.team2).count()
        self.is_confirmed = self._calculate_confirmation_status()

    def _calculate_confirmation_status(self):
        """Calculate actual confirmation status from live data."""
        team1_verified_ids = set(
            self.team1.players.filter(
                user__profile__email_verified=True
            ).values_list('id', flat=True)
        )
        team2_verified_ids = set(
            self.team2.players.filter(
                user__profile__email_verified=True
            ).values_list('id', flat=True)
        )
        confirmed_ids = set(
            self.confirmations.all().values_list('id', flat=True)
        )
        return (
            team1_verified_ids.issubset(confirmed_ids) and
            team2_verified_ids.issubset(confirmed_ids)
        )

    def save(self, *args, **kwargs):
        # Auto-determine winner based on games. Use Game.all_objects so the
        # GameManager's user/is_live filters don't hide our own children.
        # Live matches skip winner detection entirely — the scoreboard
        # endpoint flips is_live=False at match-end, then this save() picks
        # up the winner and the normal signal pipeline runs.
        if self.pk and not self.is_live:
            games_qs = Game.all_objects.filter(match_id=self.pk)
            t1_wins = games_qs.filter(winner=self.team1).count()
            t2_wins = games_qs.filter(winner=self.team2).count()
            games_to_win = (self.best_of // 2) + 1

            # Update score cache
            self.team1_score_cache = t1_wins
            self.team2_score_cache = t2_wins

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

    all_objects = models.Manager()
    objects = GameManager()

    class Meta:
        default_manager_name = 'objects'
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
        """Verify email with token. Returns True if successful, False if invalid/expired."""
        if self.email_verification_token != token:
            return False

        # Check if token has expired (24 hours)
        if self.email_verification_sent_at:
            if timezone.now() - self.email_verification_sent_at > VERIFICATION_TOKEN_EXPIRY:
                return False

        self.email_verified = True
        self.email_verification_token = ""
        self.save()
        return True

    def is_token_expired(self):
        """Check if the verification token has expired."""
        if not self.email_verification_sent_at:
            return True
        return timezone.now() - self.email_verification_sent_at > VERIFICATION_TOKEN_EXPIRY

    def __str__(self):
        return f"Profile of {self.user.username}"


class ScheduledMatch(models.Model):
    """A match scheduled for the future"""

    team1 = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="scheduled_matches_as_team1"
    )
    team2 = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="scheduled_matches_as_team2"
    )
    championship = models.ForeignKey(
        'Championship',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='scheduled_matches',
        help_text="Championship this match belongs to (if any)"
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

    # Round number for championship scheduling
    round_number = models.IntegerField(
        null=True,
        blank=True,
        help_text="Round/matchday number within a championship"
    )

    # Track if emails were sent
    notification_sent = models.BooleanField(default=False)

    # Link to actual match if scheduled match was converted
    match = models.OneToOneField(
        "Match",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_from",
        help_text="Linked match if this scheduled match was converted to a played match"
    )

    all_objects = models.Manager()
    objects = ScheduledMatchManager()

    class Meta:
        default_manager_name = 'objects'
        ordering = ["scheduled_date", "scheduled_time"]
        verbose_name = "Scheduled Match"
        verbose_name_plural = "Scheduled Matches"

    def __str__(self):
        return f"{self.team1} vs {self.team2} - {self.scheduled_date} at {self.scheduled_time}"

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
            user_player = user.player
            return user_player in (self.team1.players.all() | self.team2.players.all())
        except (AttributeError, Player.DoesNotExist):
            return False

    def user_can_edit(self, user):
        """Check if user can edit this scheduled match"""
        return self.user_can_view(user)

    @property
    def player1(self):
        """Backward-compatible property: returns first player from team1"""
        if self.team1:
            return self.team1.players.first()
        return None

    @property
    def player2(self):
        """Backward-compatible property: returns first player from team2"""
        if self.team2:
            return self.team2.players.first()
        return None

    @property
    def is_converted(self):
        """Check if this scheduled match has been converted to a played match"""
        return self.match is not None

    @property
    def is_fully_confirmed(self):
        """Check if linked match exists and is fully confirmed"""
        return bool(self.match and self.match.match_confirmed)


class EloHistory(models.Model):
    """Track Elo rating changes for each player in each match"""

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name='elo_history'
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='elo_history'
    )
    old_rating = models.IntegerField(help_text="Elo before match")
    new_rating = models.IntegerField(help_text="Elo after match")
    rating_change = models.IntegerField(help_text="Elo change (can be negative)")
    k_factor = models.FloatField(help_text="K-factor used in calculation")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Elo History"
        verbose_name_plural = "Elo Histories"
        # Prevent duplicate entries
        unique_together = ('match', 'player')

    def __str__(self):
        sign = '+' if self.rating_change >= 0 else ''
        return f"{self.player} {sign}{self.rating_change} ({self.match})"


class Achievement(models.Model):
    """Definition of an achievement/badge that players can earn."""

    class Tier(models.TextChoices):
        NONE = 'none', 'None'
        BRONZE = 'bronze', 'Bronze'
        SILVER = 'silver', 'Silver'
        GOLD = 'gold', 'Gold'

    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255)
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.NONE)
    group = models.SlugField(max_length=60)
    icon = models.CharField(max_length=60, default='award')
    threshold = models.IntegerField(default=1)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['group', 'sort_order']

    def __str__(self):
        if self.tier != self.Tier.NONE:
            return f"{self.name} ({self.get_tier_display()})"
        return self.name


class PlayerAchievement(models.Model):
    """Records when a player earned an achievement."""

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='player_achievements')
    achievement = models.ForeignKey(Achievement, on_delete=models.CASCADE, related_name='player_achievements')
    match = models.ForeignKey(
        'Match', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='achievements_awarded',
    )
    awarded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('player', 'achievement')
        ordering = ['-awarded_at']

    def __str__(self):
        return f"{self.player} - {self.achievement}"


class Championship(models.Model):
    """Championship model"""

    class Status(models.TextChoices):
        REGISTRATION = 'registration', 'Registration Open'
        SCHEDULED = 'scheduled', 'Scheduled'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    class ChampionshipType(models.TextChoices):
        SINGLES = 'singles', 'Singles (1v1)'
        DOUBLES = 'doubles', 'Doubles (2v2)'

    name = models.CharField(max_length=200, help_text="Championship name")
    description = models.TextField(blank=True, help_text="Championship description and rules")

    # Championship settings
    championship_type = models.CharField(
        max_length=20,
        choices=ChampionshipType.choices,
        default=ChampionshipType.SINGLES,
        help_text="Singles or Doubles championship"
    )
    is_public = models.BooleanField(
        default=True,
        help_text="Public championship allow anyone to register. Private championships have fixed participants."
    )
    max_participants = models.IntegerField(
        default=8,
        help_text="Maximum number of participants (players or teams)"
    )

    # Dates
    start_date = models.DateField(help_text="Championship start date")
    end_date = models.DateField(null=True, blank=True, help_text="Expected or actual end date")
    registration_deadline = models.DateField(
        null=True,
        blank=True,
        help_text="Last day to register (only for public championships)"
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.REGISTRATION
    )

    # Participants (Teams - can be single player or doubles team)
    participants = models.ManyToManyField(
        Team,
        related_name='championships',
        blank=True,
        help_text="Registered teams/players"
    )

    # Matches
    # Note: matches are linked via ForeignKey in Match model

    # Creator and timestamps
    created_by = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='championships_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Location (optional)
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Default location for championship matches"
    )

    all_objects = models.Manager()
    objects = ChampionshipManager()

    class Meta:
        default_manager_name = 'objects'
        ordering = ['-start_date', '-created_at']

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def is_registration_open(self):
        """Check if registration is still open"""
        if not self.is_public:
            return False
        if self.status != self.Status.REGISTRATION:
            return False
        if self.registration_deadline:
            from django.utils import timezone
            return timezone.now().date() <= self.registration_deadline
        return True

    @property
    def current_participants_count(self):
        """Get current number of participants"""
        return self.participants.count()

    @property
    def is_full(self):
        """Check if championship is at capacity"""
        return self.current_participants_count >= self.max_participants

    def can_register(self, team):
        """Check if a team can register for this championship"""
        if not self.is_registration_open:
            return False
        if self.is_full:
            return False
        if self.participants.filter(pk=team.pk).exists():
            return False
        # Check team size matches championship type
        team_size = team.players.count()
        if self.championship_type == self.ChampionshipType.SINGLES and team_size != 1:
            return False
        if self.championship_type == self.ChampionshipType.DOUBLES and team_size != 2:
            return False
        return True

    def register_team(self, team):
        """Register a team for the championship"""
        if self.can_register(team):
            self.participants.add(team)
            return True
        return False

    def generate_schedule(self):
        """
        Generate round-robin schedule for the championship using the circle method.

        Creates home and away rounds (andata e ritorno). Each round has n/2 matches
        where every team plays exactly once. Rounds are spaced 7 days apart.
        """
        from datetime import timedelta, time

        participants = list(self.participants.all())
        n = len(participants)

        if n < 2:
            return False

        # Delete existing scheduled matches for this championship
        ScheduledMatch.all_objects.filter(championship=self).delete()

        # Circle method for round-robin scheduling
        # If odd number of participants, add a "bye" (None)
        teams = list(participants)
        if n % 2 == 1:
            teams.append(None)  # bye

        num_teams = len(teams)
        num_rounds = num_teams - 1

        # Generate rounds using circle method:
        # Fix first team, rotate the rest
        rounds = []  # List of (round_number, [(team1, team2), ...])

        for round_idx in range(num_rounds):
            round_matches = []
            for i in range(num_teams // 2):
                t1 = teams[i]
                t2 = teams[num_teams - 1 - i]
                if t1 is not None and t2 is not None:
                    round_matches.append((t1, t2))
            rounds.append((round_idx + 1, round_matches))

            # Rotate: keep teams[0] fixed, rotate the rest clockwise
            teams = [teams[0]] + [teams[-1]] + teams[1:-1]

        # Create home leg (andata) and away leg (ritorno)
        all_rounds = []
        for round_num, matches in rounds:
            all_rounds.append((round_num, matches))
        for round_num, matches in rounds:
            # Swap home/away for return leg
            away_matches = [(t2, t1) for t1, t2 in matches]
            all_rounds.append((round_num + num_rounds, away_matches))

        # Create scheduled matches, 1 round per week
        match_time = time(hour=18, minute=0)

        matches_to_create = []
        for round_num, round_matches in all_rounds:
            round_date = self.start_date + timedelta(weeks=round_num - 1)
            for team1, team2 in round_matches:
                matches_to_create.append(ScheduledMatch(
                    championship=self,
                    team1=team1,
                    team2=team2,
                    scheduled_date=round_date,
                    scheduled_time=match_time,
                    location=self.location,
                    created_by=self.created_by,
                    round_number=round_num,
                ))
        ScheduledMatch.all_objects.bulk_create(matches_to_create)

        # Set end_date based on last round
        if all_rounds:
            last_round_num = all_rounds[-1][0]
            self.end_date = self.start_date + timedelta(weeks=last_round_num - 1)
            self.save(update_fields=['end_date'])

        return True

    def get_standings(self):
        """
        Calculate championship standings.

        Ranking criteria:
        1. Points (3 for win, 0 for loss)
        2. Game difference (games won - games lost)
        3. Total games won
        4. Total games lost
        """
        standings = []

        # Fetch all championship matches once with prefetch
        all_matches_qs = Match.all_objects.filter(
            championship=self,
        ).select_related('team1', 'team2', 'winner').prefetch_related(
            'games', 'confirmations',
            'team1__players', 'team1__players__user__profile',
            'team2__players', 'team2__players__user__profile',
        )
        # Filter to confirmed matches in Python using prefetched data
        all_matches = [m for m in all_matches_qs if m.match_confirmed]

        # Pre-compute game scores using prefetched data (avoids N+1)
        match_scores = {}
        for match in all_matches:
            t1_score = sum(1 for g in match.games.all() if g.winner_id == match.team1_id)
            t2_score = sum(1 for g in match.games.all() if g.winner_id == match.team2_id)
            match_scores[match.pk] = (t1_score, t2_score)

        participants = self.participants.prefetch_related('players').all()

        for team in participants:
            played = 0
            wins = 0
            losses = 0
            games_won = 0
            games_lost = 0

            for match in all_matches:
                if match.team1_id != team.pk and match.team2_id != team.pk:
                    continue

                played += 1
                is_team1 = match.team1_id == team.pk
                t1_score, t2_score = match_scores[match.pk]
                team_score = t1_score if is_team1 else t2_score
                opponent_score = t2_score if is_team1 else t1_score

                games_won += team_score
                games_lost += opponent_score

                if match.winner_id == team.pk:
                    wins += 1
                else:
                    losses += 1

            points = (wins * 3)
            game_difference = games_won - games_lost

            standings.append({
                'team': team,
                'played': played,
                'wins': wins,
                'losses': losses,
                'games_won': games_won,
                'games_lost': games_lost,
                'game_difference': game_difference,
                'points': points,
            })

        # Sort by: points (desc), game difference (desc), games won (desc), games lost (asc)
        standings.sort(
            key=lambda x: (x['points'], x['game_difference'], x['games_won'], -x['games_lost']),
            reverse=True
        )

        return standings

    def check_completion(self):
        """Check if all championship matches are completed and confirmed.
        If so, auto-transition to 'completed' status.
        """
        if self.status != self.Status.IN_PROGRESS:
            return False

        total_scheduled = ScheduledMatch.all_objects.filter(championship=self).count()
        if total_scheduled == 0:
            return False

        # Check all scheduled matches are converted
        converted = ScheduledMatch.all_objects.filter(
            championship=self, match__isnull=False
        ).count()
        if converted < total_scheduled:
            return False

        # Check all linked matches are confirmed
        championship_matches = Match.all_objects.filter(
            championship=self
        ).prefetch_related(
            'confirmations',
            'team1__players__user__profile',
            'team2__players__user__profile',
        )
        for match in championship_matches:
            if not match.match_confirmed:
                return False

        self.status = self.Status.COMPLETED
        self.save(update_fields=['status'])
        return True

    def user_can_view(self, user):
        """Check if user can view this championship"""
        if not user or not user.is_authenticated:
            return False
        if self.is_public:
            return True
        if user.is_staff or user.is_superuser:
            return True
        # Check if user is a participant
        try:
            player = user.player
            return self.participants.filter(players=player).exists()
        except (AttributeError, Player.DoesNotExist):
            return False

    def user_can_edit(self, user):
        """Check if user can edit this championship"""
        if not user or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        try:
            player = user.player
            return self.created_by == player
        except (AttributeError, Player.DoesNotExist):
            return False

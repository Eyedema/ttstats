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

    objects = ScheduledMatchManager()

    class Meta:
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


class Championship(models.Model):
    """Championship model"""

    CHAMPIONSHIP_STATUS = [
        ('registration', 'Registration Open'),
        ('scheduled', 'Scheduled'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    CHAMPIONSHIP_TYPE = [
        ('singles', 'Singles (1v1)'),
        ('doubles', 'Doubles (2v2)'),
    ]

    name = models.CharField(max_length=200, help_text="Championship name")
    description = models.TextField(blank=True, help_text="Championship description and rules")

    # Championship settings
    championship_type = models.CharField(
        max_length=20,
        choices=CHAMPIONSHIP_TYPE,
        default='singles',
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
        choices=CHAMPIONSHIP_STATUS,
        default='registration'
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

    class Meta:
        ordering = ['-start_date', '-created_at']

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def is_registration_open(self):
        """Check if registration is still open"""
        if not self.is_public:
            return False
        if self.status != 'registration':
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
        if self.championship_type == 'singles' and team_size != 1:
            return False
        if self.championship_type == 'doubles' and team_size != 2:
            return False
        return True

    def register_team(self, team):
        """Register a team for the championship"""
        if self.can_register(team):
            self.participants.add(team)
            return True
        return False

    def generate_schedule(self):
        """Generate round-robin schedule for the championship"""
        from datetime import timedelta
        from django.utils import timezone

        participants = list(self.participants.all())
        n = len(participants)

        if n < 2:
            return False

        # Delete existing scheduled matches for this championship
        ScheduledMatch.objects.filter(championship=self).delete()

        # Generate round-robin pairings
        matches = []

        # Home and away (andata e ritorno)
        for round_num in range(2):  # 0 = andata, 1 = ritorno
            for i in range(n):
                for j in range(i + 1, n):
                    if round_num == 0:
                        team1, team2 = participants[i], participants[j]
                    else:
                        team1, team2 = participants[j], participants[i]

                    matches.append((team1, team2))

        # Create scheduled matches
        # Space matches out by days starting from start_date
        current_date = self.start_date
        match_time = timezone.now().time().replace(hour=18, minute=0, second=0)

        for idx, (team1, team2) in enumerate(matches):
            ScheduledMatch.objects.create(
                championship=self,
                team1=team1,
                team2=team2,
                scheduled_date=current_date,
                scheduled_time=match_time,
                location=self.location,
                created_by=self.created_by
            )
            # Space matches - 1 per day for simplicity
            # Adjust this logic based on your needs
            current_date += timedelta(days=1)

        return True

    def get_standings(self):
        """
        Calculate championship standings.

        Ranking criteria:
        1. Points (3 for win, 0 for loss)
        2. Head-to-head record (if tied on points)
        3. Games difference (games won - games lost)
        4. Total games won
        5. Total games lost
        """
        from django.db.models import Q

        standings = []

        for team in self.participants.all():
            # Get all confirmed matches for this team in this championship
            matches = Match.objects.filter(
                championship=self,
            ).filter(
                Q(team1=team) | Q(team2=team)
            ).select_related('team1', 'team2', 'winner').prefetch_related(
                'confirmations', 'games'
            )

            # Filter to confirmed matches only
            confirmed_matches = [m for m in matches if m.match_confirmed]

            played = len(confirmed_matches)
            wins = 0
            losses = 0
            games_won = 0
            games_lost = 0

            for match in confirmed_matches:
                is_team1 = match.team1 == team
                team_score = match.team1_score if is_team1 else match.team2_score
                opponent_score = match.team2_score if is_team1 else match.team1_score

                games_won += team_score
                games_lost += opponent_score

                if match.winner == team:
                    wins += 1
                else:
                    losses += 1

            points = (wins * 3)
            goal_difference = games_won - games_lost

            standings.append({
                'team': team,
                'played': played,
                'wins': wins,
                'losses': losses,
                'games_won': games_won,
                'games_lost': games_lost,
                'goal_difference': goal_difference,
                'points': points,
            })

        # Sort by: points (desc), goal difference (desc), goals for (desc), goals against (asc)
        standings.sort(
            key=lambda x: (x['points'], x['goal_difference'], x['games_won'], -x['games_lost']),
            reverse=True
        )

        return standings

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
        except:
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
        except:
            return False

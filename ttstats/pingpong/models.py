# Create your models here.
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from . import match_state
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


def format_side_label(players):
    """Human-readable name for a side: "Ada", "Ada and Bob", "Ada and Bob (+1)"."""
    players = list(players)
    if not players:
        return ""
    if len(players) == 1:
        return str(players[0])
    if len(players) == 2:
        return f"{players[0]} and {players[1]}"
    return f"{players[0]} and {players[1]} (+{len(players) - 2})"


class SideLabelMixin:
    """Side labels for anything exposing players_on(side)."""

    def side_label(self, side):
        return format_side_label(self.players_on(side)) or f"Side {int(side)}"

    @property
    def side1_label(self):
        return self.side_label(Side.ONE)

    @property
    def side2_label(self):
        return self.side_label(Side.TWO)


class Side(models.IntegerChoices):
    """Which half of a match a player or result belongs to.

    Replaces pointing at a Team object: a side is a position within one match,
    not an entity, so an integer says what an FK could only imply.
    """

    ONE = 1, "Side 1"
    TWO = 2, "Side 2"


class Match(SideLabelMixin, models.Model):
    """Individual match between two players"""

    is_double = models.BooleanField(default=False)

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

    winner_side = models.PositiveSmallIntegerField(
        choices=Side.choices, null=True, blank=True, db_index=True
    )

    # Championship entry per side, so standings never have to reverse-map a
    # match to an entry by intersecting player sets.
    side1_entry = models.ForeignKey(
        'ChampionshipEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='%(class)s_as_side1',
    )
    side2_entry = models.ForeignKey(
        'ChampionshipEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='%(class)s_as_side2',
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

    # Set the first time the "match confirmed, here is your Elo" push goes
    # out. Three separate signal paths can reach that point for the same
    # match, and two of them can run concurrently when both players confirm
    # at once, so exactly-once needs a persisted flag rather than an in-memory
    # one. Written with a conditional queryset .update(), which makes it an
    # atomic compare-and-set -- see notifications.notify_match_confirmed.
    result_notified_at = models.DateTimeField(null=True, blank=True, editable=False)

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
            return self.participants.filter(player__user_id=user.pk).exists()
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
        return f"{self.side1_label} vs {self.side2_label} - {self.date_played.date()}"

    @property
    def side1_score(self):
        """Games won by side 1. Reads the cache recompute() maintains."""
        return self.team1_score_cache

    @property
    def side2_score(self):
        return self.team2_score_cache

    # Legacy names, kept because templates and emails read them.
    team1_score = side1_score
    team2_score = side2_score

    @property
    def winner_label(self):
        return self.side_label(self.winner_side) if self.winner_side else ""

    def players_on(self, side):
        """Players on one side of the match, read from MatchParticipant."""
        return Player.objects.filter(
            match_participations__match_id=self.pk,
            match_participations__side=side,
        )

    @property
    def all_players(self):
        """Everyone in the match, both sides."""
        return Player.objects.filter(match_participations__match_id=self.pk)

    @property
    def side1_players(self):
        return self.players_on(Side.ONE)

    @property
    def side2_players(self):
        return self.players_on(Side.TWO)

    def _verified_player_ids(self, side):
        """Ids of players on ``side`` whose email is verified."""
        return set(
            self.players_on(side)
            .filter(user__profile__email_verified=True)
            .values_list("id", flat=True)
        )

    def _confirmed_player_ids(self):
        return set(self.confirmations.values_list("id", flat=True))

    @property
    def team1_confirmed(self):
        """All verified Team 1 members have confirmed"""
        return match_state.side_confirmed(
            self._verified_player_ids(Side.ONE), self._confirmed_player_ids()
        )

    @property
    def team2_confirmed(self):
        """All verified Team 2 members have confirmed"""
        return match_state.side_confirmed(
            self._verified_player_ids(Side.TWO), self._confirmed_player_ids()
        )

    @property
    def match_confirmed(self):
        """Every verified player on both teams has confirmed."""
        return match_state.confirmation_complete(
            self._verified_player_ids(Side.ONE),
            self._verified_player_ids(Side.TWO),
            self._confirmed_player_ids(),
        )

    def should_auto_confirm(self):
        return match_state.should_auto_confirm(
            has_winner=bool(self.winner_side),
            already_confirmed=self.match_confirmed,
            side1_has_verified=bool(self._verified_player_ids(Side.ONE)),
            side2_has_verified=bool(self._verified_player_ids(Side.TWO)),
        )

    def get_unverified_players(self):
        unverified = []

        all_players = self.all_players

        for player in all_players:
            if not player.user or not player.user.profile.email_verified:
                unverified.append(player)

        return unverified

    def _game_wins(self):
        """Games won per side. Uses Game.all_objects so the GameManager's
        user/is_live filters don't hide our own children.
        """
        games_qs = Game.all_objects.filter(match_id=self.pk)
        return (
            games_qs.filter(winner_side=Side.ONE).count(),
            games_qs.filter(winner_side=Side.TWO).count(),
        )

    def recompute(self, save=True):
        """Single source of truth for winner, score caches and is_confirmed.

        With ``save=True`` the new values are written with a queryset update so
        no pre/post_save signal fires -- callers are usually inside one. With
        ``save=False`` the instance is only mutated, for use from ``save()``.

        Live matches skip winner detection: the scoreboard endpoint flips
        is_live=False at match-end, then the normal pipeline picks the winner up.
        """
        if not self.pk:
            return

        if not self.is_live:
            side1_wins, side2_wins = self._game_wins()
            self.team1_score_cache = side1_wins
            self.team2_score_cache = side2_wins

            decided = match_state.winner_side(side1_wins, side2_wins, self.best_of)
            if decided is not None:
                self.winner_side = decided
            # An undecided result deliberately leaves an existing winner in
            # place rather than clearing it, matching long-standing behaviour.

        self.is_confirmed = self.match_confirmed

        if save:
            Match.all_objects.filter(pk=self.pk).update(
                team1_score_cache=self.team1_score_cache,
                team2_score_cache=self.team2_score_cache,
                winner_side=self.winner_side,
                is_confirmed=self.is_confirmed,
            )

    def save(self, *args, **kwargs):
        if self.pk and not self.is_live:
            self.recompute(save=False)
        super().save(*args, **kwargs)


class MatchConfirmation(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE)
    player = models.ForeignKey(Player, on_delete=models.CASCADE)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('match', 'player')  # Players need to confirm only once


class MatchParticipant(models.Model):
    """A player's place in a match. Replaces the Team indirection."""

    match = models.ForeignKey(
        Match, on_delete=models.CASCADE, related_name="participants"
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="match_participations"
    )
    side = models.PositiveSmallIntegerField(choices=Side.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["match", "player"], name="uniq_match_player"
            )
        ]
        indexes = [
            models.Index(fields=["match", "side"]),
            models.Index(fields=["player", "match"]),
        ]

    def __str__(self):
        return f"{self.player} on side {self.side} of match {self.match_id}"


class Game(models.Model):
    """Individual game within a match"""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="games")
    game_number = models.IntegerField(help_text="1st game, 2nd game, etc.")
    team1_score = models.IntegerField(default=0)
    team2_score = models.IntegerField(default=0)

    winner_side = models.PositiveSmallIntegerField(
        choices=Side.choices, null=True, blank=True
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

    @property
    def winner_label(self):
        """Name of the side that won this game, resolved via the parent match."""
        if not self.winner_side:
            return ""
        return self.match.side_label(self.winner_side)

    def save(self, *args, **kwargs):
        # Auto-determine which side won this game
        if self.team1_score > self.team2_score:
            self.winner_side = Side.ONE
        elif self.team2_score > self.team1_score:
            self.winner_side = Side.TWO

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


class ScheduledMatch(SideLabelMixin, models.Model):
    """A match scheduled for the future"""

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
    # Championship entry per side, so standings never have to reverse-map a
    # match to an entry by intersecting player sets.
    side1_entry = models.ForeignKey(
        'ChampionshipEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='%(class)s_as_side1',
    )
    side2_entry = models.ForeignKey(
        'ChampionshipEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='%(class)s_as_side2',
    )

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
        return f"{self.side1_label} vs {self.side2_label} - {self.scheduled_date} at {self.scheduled_time}"

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
            return self.participants.filter(player=user_player).exists()
        except (AttributeError, Player.DoesNotExist):
            return False

    def user_can_edit(self, user):
        """Check if user can edit this scheduled match"""
        return self.user_can_view(user)

    def players_on(self, side):
        """Players on one side, read from ScheduledMatchParticipant."""
        return Player.objects.filter(
            scheduled_match_participations__scheduled_match_id=self.pk,
            scheduled_match_participations__side=side,
        )

    @property
    def side1_players(self):
        return self.players_on(Side.ONE)

    @property
    def side2_players(self):
        return self.players_on(Side.TWO)

    @property
    def is_converted(self):
        """Check if this scheduled match has been converted to a played match"""
        return self.match is not None

    @property
    def is_fully_confirmed(self):
        """Check if linked match exists and is fully confirmed"""
        return bool(self.match and self.match.match_confirmed)


class ScheduledMatchParticipant(models.Model):
    """A player's place in a scheduled match. Mirrors MatchParticipant."""

    scheduled_match = models.ForeignKey(
        ScheduledMatch, on_delete=models.CASCADE, related_name="participants"
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="scheduled_match_participations"
    )
    side = models.PositiveSmallIntegerField(choices=Side.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scheduled_match", "player"],
                name="uniq_scheduled_match_player",
            )
        ]
        indexes = [models.Index(fields=["scheduled_match", "side"])]

    def __str__(self):
        return (
            f"{self.player} on side {self.side} "
            f"of scheduled match {self.scheduled_match_id}"
        )


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
        return self.entries.count()

    @property
    def is_full(self):
        """Check if championship is at capacity"""
        return self.current_participants_count >= self.max_participants

    @property
    def required_entry_size(self):
        return 1 if self.championship_type == self.ChampionshipType.SINGLES else 2

    def can_register(self, players):
        """Whether ``players`` may enter as one competitor."""
        players = list(players)
        if not self.is_registration_open:
            return False
        if self.is_full:
            return False
        if len(players) != self.required_entry_size:
            return False
        if len({p.pk for p in players}) != len(players):
            return False
        # One entry per player per championship (also a DB constraint).
        if self.entry_members.filter(player__in=players).exists():
            return False
        return True

    def register_entry(self, players, display_name=""):
        """Create an entry for ``players``. Returns the entry, or None."""
        players = list(players)
        if not self.can_register(players):
            return None
        entry = ChampionshipEntry.objects.create(
            championship=self, display_name=display_name
        )
        ChampionshipEntryMember.objects.bulk_create(
            [
                ChampionshipEntryMember(entry=entry, player=p, championship=self)
                for p in players
            ]
        )
        return entry

    def generate_schedule(self):
        """Generate the round-robin schedule (home and away legs).

        The pairing itself lives in championship_scheduling as a pure
        function; this method only turns pairings into rows.
        """
        from datetime import timedelta, time

        from .championship_scheduling import round_robin_double_rounds

        entries = list(self.entries.prefetch_related("members__player"))
        if len(entries) < 2:
            return False

        ScheduledMatch.all_objects.filter(championship=self).delete()

        all_rounds = round_robin_double_rounds(entries)
        match_time = time(hour=18, minute=0)

        to_create = []
        for round_num, pairings in all_rounds:
            round_date = self.start_date + timedelta(weeks=round_num - 1)
            for entry1, entry2 in pairings:
                to_create.append(ScheduledMatch(
                    championship=self,
                    side1_entry=entry1,
                    side2_entry=entry2,
                    scheduled_date=round_date,
                    scheduled_time=match_time,
                    location=self.location,
                    created_by=self.created_by,
                    round_number=round_num,
                ))
        created = ScheduledMatch.all_objects.bulk_create(to_create)

        # bulk_create does not fire post_save, so the participant rows the
        # save hook would have written have to be built explicitly.
        participant_rows = [
            ScheduledMatchParticipant(
                scheduled_match=scheduled, player_id=member.player_id, side=side
            )
            for scheduled in created
            for side, entry in (
                (Side.ONE, scheduled.side1_entry),
                (Side.TWO, scheduled.side2_entry),
            )
            for member in entry.members.all()
        ]
        ScheduledMatchParticipant.objects.bulk_create(
            participant_rows, ignore_conflicts=True
        )

        if all_rounds:
            last_round_num = all_rounds[-1][0]
            self.end_date = self.start_date + timedelta(weeks=last_round_num - 1)
            self.save(update_fields=['end_date'])

        return True

    def get_standings(self):
        """Calculate championship standings.

        Ranked by points (3 per win), then game difference, then games won,
        then fewest games lost.
        """
        matches = list(
            Match.all_objects.filter(championship=self).prefetch_related(
                'confirmations', 'participants__player__user__profile'
            )
        )
        confirmed = [m for m in matches if m.match_confirmed]

        standings = []
        for entry in self.entries.prefetch_related('members__player'):
            played = wins = losses = games_won = games_lost = 0

            for match in confirmed:
                if match.side1_entry_id == entry.pk:
                    my_side, mine, theirs = (
                        Side.ONE, match.team1_score_cache, match.team2_score_cache
                    )
                elif match.side2_entry_id == entry.pk:
                    my_side, mine, theirs = (
                        Side.TWO, match.team2_score_cache, match.team1_score_cache
                    )
                else:
                    continue

                played += 1
                games_won += mine
                games_lost += theirs
                if match.winner_side == my_side:
                    wins += 1
                else:
                    losses += 1

            standings.append({
                'entry': entry,
                'team': entry,  # legacy key, still used by templates
                'played': played,
                'wins': wins,
                'losses': losses,
                'games_won': games_won,
                'games_lost': games_lost,
                'game_difference': games_won - games_lost,
                'points': wins * 3,
            })

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
            'participants__player__user__profile',
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
            return self.entry_members.filter(player=player).exists()
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


class ChampionshipEntry(models.Model):
    """One competitor in a championship: a player, or a pair for doubles.

    Replaces registering a Team. An entry belongs to exactly one championship,
    so the same people entering two championships are two entries -- which is
    what lets the database enforce one entry per player per championship.
    """

    championship = models.ForeignKey(
        Championship, on_delete=models.CASCADE, related_name="entries"
    )
    display_name = models.CharField(
        max_length=100, blank=True, help_text="Optional team name for this entry"
    )
    seed = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["seed", "created_at"]
        verbose_name_plural = "championship entries"

    @property
    def players(self):
        return Player.objects.filter(championship_entries__entry=self)

    def __str__(self):
        return self.display_name or format_side_label(self.players) or "Entry"


class ChampionshipEntryMember(models.Model):
    """A player belonging to a championship entry.

    ``championship`` is denormalized from ``entry`` so the database can enforce
    that a player appears in at most one entry per championship -- impossible
    with the old M2M(Team), where a player could register two singleton teams.
    """

    entry = models.ForeignKey(
        ChampionshipEntry, on_delete=models.CASCADE, related_name="members"
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="championship_entries"
    )
    championship = models.ForeignKey(
        Championship, on_delete=models.CASCADE, related_name="entry_members"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "player"], name="uniq_entry_player"
            ),
            models.UniqueConstraint(
                fields=["championship", "player"], name="uniq_championship_player"
            ),
        ]

    def __str__(self):
        return f"{self.player} in {self.entry}"


class NotificationKind(models.TextChoices):
    """The push notifications a user can be sent, and can switch off.

    The value is stored on NotificationPreference as the suffix of a boolean
    field (`push_<value>`), so adding a kind means adding the matching field
    and a migration. `NotificationPreference.wants()` does the lookup, so no
    caller needs to know that.
    """

    MATCH_CONFIRMATION = 'match_confirmation', 'A match is waiting for your confirmation'
    MATCH_RESULT = 'match_result', 'A match you played was confirmed'
    SCHEDULED_MATCH = 'scheduled_match', 'A match was scheduled for you'
    LEADERBOARD_OVERTAKE = 'leaderboard_overtake', 'Someone passed you on the leaderboard'


class NotificationPreference(models.Model):
    """Per-user opt-outs, one boolean per NotificationKind.

    Rows are created by the same post_save signal that creates UserProfile,
    but `for_user()` also creates on demand so a user who predates this
    feature never hits a missing-row crash.
    """

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='notification_preference'
    )
    push_match_confirmation = models.BooleanField(default=True)
    push_match_result = models.BooleanField(default=True)
    push_scheduled_match = models.BooleanField(default=True)
    # Off by default: it is the one notification that fires without the user
    # having done anything, so opting in should be a deliberate act.
    push_leaderboard_overtake = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def for_user(cls, user):
        prefs, _ = cls.objects.get_or_create(user=user)
        return prefs

    def wants(self, kind):
        """True if the user still wants pushes of this kind.

        An unknown kind returns True rather than False: a new notification
        type shipped without its preference field should be noisy and get
        noticed, not silently deliver to nobody.
        """
        return getattr(self, f'push_{kind}', True)

    def __str__(self):
        return f"Notification preferences for {self.user.username}"


class PushSubscription(models.Model):
    """One browser's Web Push endpoint for one user.

    A user has as many rows as they have installed browsers/devices. The
    endpoint URL is the identity: it is what the push service gave the
    browser, and re-subscribing on the same device returns the same one, so
    it is unique and upserted on rather than duplicated.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='push_subscriptions'
    )
    # Push endpoints are long and have no meaningful bound, hence TextField.
    endpoint = models.TextField(unique=True)
    # The two keys from the browser's PushSubscription, base64url, unpadded.
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    # Reset to 0 on every success. Only a 404/410 from the push service
    # deletes a row; this counter is for diagnosing the flaky ones.
    failure_count = models.IntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user'])]

    @property
    def subscription_info(self):
        """The dict shape pywebpush expects, rebuilt from the stored columns."""
        return {
            'endpoint': self.endpoint,
            'keys': {'p256dh': self.p256dh, 'auth': self.auth},
        }

    def __str__(self):
        return f"Push subscription for {self.user.username} ({self.endpoint[:40]}...)"

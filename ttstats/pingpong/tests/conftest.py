import factory
import pytest
from datetime import date, time, timedelta
from django.contrib.auth.models import User, AnonymousUser
from django.core.cache import cache as django_cache
from django.test import Client
from factory.django import DjangoModelFactory

from pingpong.models import Game, Location, Match, MatchConfirmation, Player, ScheduledMatch, Team


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

class UserFactory(DjangoModelFactory):
    class Meta:
        model = User

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        password = kwargs.pop("password", "testpass123")
        manager = cls._get_manager(model_class)
        return manager.create_user(*args, password=password, **kwargs)


class LocationFactory(DjangoModelFactory):
    class Meta:
        model = Location

    name = factory.Sequence(lambda n: f"Location {n}")
    address = factory.Faker("address")
    notes = ""


class PlayerFactory(DjangoModelFactory):
    class Meta:
        model = Player

    name = factory.Sequence(lambda n: f"Player {n}")
    nickname = ""
    playing_style = "normal"

    class Params:
        with_user = factory.Trait(
            user=factory.SubFactory(UserFactory),
        )


class TeamFactory(DjangoModelFactory):
    """Factory for creating Team instances.

    Usage:
        # Create an empty team (you'll set players after)
        team = TeamFactory()
        team.players.set([player1, player2])

        # Create a team with players via trait
        team = TeamFactory(players=[player1, player2])
    """
    class Meta:
        model = Team

    name = ""

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        players = kwargs.pop('players', None)
        team = super()._create(model_class, *args, **kwargs)
        if players:
            team.players.set(players)
        return team


class MatchFactory(DjangoModelFactory):
    """Factory for Match. Supports backwards-compatible player1/player2 kwargs.

    Usage:
        # Original style (creates single-player teams automatically):
        match = MatchFactory(player1=p1, player2=p2)

        # Team style:
        match = MatchFactory(team1=team1, team2=team2)

        # With confirmation:
        match = MatchFactory(player1=p1, player2=p2, confirmed=True)

        # For doubles:
        match = MatchFactory(
            is_double=True,
            team1_players=[p1, p2],
            team2_players=[p3, p4]
        )
    """
    class Meta:
        model = Match

    best_of = 5
    match_type = "casual"

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        # Extract special kwargs
        player1 = kwargs.pop('player1', None)
        player2 = kwargs.pop('player2', None)
        team1 = kwargs.pop('team1', None)
        team2 = kwargs.pop('team2', None)
        team1_players = kwargs.pop('team1_players', None)
        team2_players = kwargs.pop('team2_players', None)
        confirmed = kwargs.pop('confirmed', False)

        # Remove old-style confirmation kwargs (no longer exist on model)
        kwargs.pop('player1_confirmed', None)
        kwargs.pop('player2_confirmed', None)

        # Handle team creation based on what was provided
        if team1 is None:
            if team1_players:
                team1 = Team.objects.create()
                team1.players.set(team1_players)
            elif player1:
                team1 = Team.objects.create()
                team1.players.set([player1])
            else:
                # Create default player with user for team1
                default_player1 = PlayerFactory(with_user=True)
                team1 = Team.objects.create()
                team1.players.set([default_player1])

        if team2 is None:
            if team2_players:
                team2 = Team.objects.create()
                team2.players.set(team2_players)
            elif player2:
                team2 = Team.objects.create()
                team2.players.set([player2])
            else:
                # Create default player with user for team2
                default_player2 = PlayerFactory(with_user=True)
                team2 = Team.objects.create()
                team2.players.set([default_player2])

        kwargs['team1'] = team1
        kwargs['team2'] = team2

        # Create the match
        match = super()._create(model_class, *args, **kwargs)

        # Handle confirmations if requested
        if confirmed:
            confirm_match(match)

        return match


class GameFactory(DjangoModelFactory):
    class Meta:
        model = Game

    match = factory.SubFactory(MatchFactory)
    game_number = factory.Sequence(lambda n: n + 1)
    team1_score = 11
    team2_score = 5


class ScheduledMatchFactory(DjangoModelFactory):
    """Factory for ScheduledMatch. Supports backwards-compatible player1/player2 kwargs.

    Usage:
        # Original style (creates single-player teams automatically):
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        # Team style:
        sm = ScheduledMatchFactory(team1=team1, team2=team2)
    """
    class Meta:
        model = ScheduledMatch

    scheduled_date = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    scheduled_time = factory.LazyFunction(lambda: time(14, 0))

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        # Extract special kwargs
        player1 = kwargs.pop('player1', None)
        player2 = kwargs.pop('player2', None)
        team1 = kwargs.pop('team1', None)
        team2 = kwargs.pop('team2', None)
        team1_players = kwargs.pop('team1_players', None)
        team2_players = kwargs.pop('team2_players', None)

        # Handle team creation based on what was provided
        if team1 is None:
            if team1_players:
                team1 = Team.objects.create()
                team1.players.set(team1_players)
            elif player1:
                team1 = Team.objects.create()
                team1.players.set([player1])
            else:
                # Create default player with user for team1
                default_player1 = PlayerFactory(with_user=True)
                team1 = Team.objects.create()
                team1.players.set([default_player1])

        if team2 is None:
            if team2_players:
                team2 = Team.objects.create()
                team2.players.set(team2_players)
            elif player2:
                team2 = Team.objects.create()
                team2.players.set([player2])
            else:
                # Create default player with user for team2
                default_player2 = PlayerFactory(with_user=True)
                team2 = Team.objects.create()
                team2.players.set([default_player2])

        kwargs['team1'] = team1
        kwargs['team2'] = team2

        return super()._create(model_class, *args, **kwargs)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_match_players(match):
    """Get (player1, player2) tuple for singles matches.

    Returns the first player from each team.
    For doubles, returns (team1 first player, team2 first player).
    """
    return match.team1.players.first(), match.team2.players.first()


def confirm_match(match, players=None):
    """Create MatchConfirmation records for specified (or all) players.

    Args:
        match: Match instance to confirm
        players: Optional list of players to confirm. If None, confirms all
                 players from both teams.

    Returns:
        List of created MatchConfirmation records
    """
    if players is None:
        players = list(match.team1.players.all()) + list(match.team2.players.all())

    confirmations = []
    for player in players:
        confirmation, created = MatchConfirmation.objects.get_or_create(
            match=match,
            player=player
        )
        if created:
            confirmations.append(confirmation)

    return confirmations


def confirm_match_silent(match, players=None):
    """Create MatchConfirmation records WITHOUT triggering signals.

    Use this when you want to set up test data without triggering Elo updates.
    Uses bulk_create which bypasses Django signals.

    Args:
        match: Match instance to confirm
        players: Optional list of players to confirm. If None, confirms all
                 players from both teams.

    Returns:
        List of created MatchConfirmation records
    """
    if players is None:
        players = list(match.team1.players.all()) + list(match.team2.players.all())

    confirmations = [
        MatchConfirmation(match=match, player=player)
        for player in players
    ]
    return MatchConfirmation.objects.bulk_create(confirmations, ignore_conflicts=True)


def confirm_team(match, team_num):
    """Confirm all players from a specific team.

    Args:
        match: Match instance
        team_num: 1 or 2 to indicate which team to confirm

    Returns:
        List of created MatchConfirmation records
    """
    team = match.team1 if team_num == 1 else match.team2
    return confirm_match(match, players=list(team.players.all()))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear Django cache before each test to prevent cross-test contamination."""
    django_cache.clear()
    yield
    django_cache.clear()


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def staff_user(db):
    return UserFactory(is_staff=True)


@pytest.fixture
def superuser(db):
    return UserFactory(is_staff=True, is_superuser=True)


@pytest.fixture
def verified_user(db):
    u = UserFactory()
    u.profile.email_verified = True
    u.profile.save()
    return u


@pytest.fixture
def player(db):
    return PlayerFactory()


@pytest.fixture
def player_with_user(db):
    return PlayerFactory(with_user=True)


@pytest.fixture
def location(db):
    return LocationFactory()


@pytest.fixture
def match(db):
    return MatchFactory()


@pytest.fixture
def complete_match(db):
    """A match with 3 games where team1 wins (best of 5)."""
    m = MatchFactory()
    GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
    GameFactory(match=m, game_number=2, team1_score=11, team2_score=9)
    GameFactory(match=m, game_number=3, team1_score=11, team2_score=7)
    m.refresh_from_db()
    return m


@pytest.fixture
def auth_client(db):
    """Return a function that creates a logged-in test client for a given user."""
    def _make(user):
        c = Client()
        c.force_login(user)
        return c
    return _make

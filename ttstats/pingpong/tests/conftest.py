import factory
import pytest
from datetime import date, time, timedelta
from django.contrib.auth.models import User, AnonymousUser
from django.core.cache import cache as django_cache
from django.test import Client
from factory.django import DjangoModelFactory

from pingpong.models import Championship, ChampionshipEntry, ChampionshipEntryMember, Game, Location, Match, MatchConfirmation, Player, ScheduledMatch, Side
from pingpong.services import set_match_sides, set_scheduled_match_sides


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


class MatchFactory(DjangoModelFactory):
    """Factory for Match. Supports backwards-compatible player1/player2 kwargs.

    Usage:
        # Original style (creates single-player teams automatically):
        match = MatchFactory(player1=p1, player2=p2)

        # Team style:
        match = MatchFactory(team1_players=[p1], team2_players=[p2])

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
        team1_players = kwargs.pop('team1_players', None)
        team2_players = kwargs.pop('team2_players', None)
        confirmed = kwargs.pop('confirmed', False)

        # Remove old-style confirmation kwargs (no longer exist on model)
        kwargs.pop('player1_confirmed', None)
        kwargs.pop('player2_confirmed', None)

        side1 = team1_players or [player1 or PlayerFactory(with_user=True)]
        side2 = team2_players or [player2 or PlayerFactory(with_user=True)]

        match = super()._create(model_class, *args, **kwargs)
        set_match_sides(match, side1, side2)
        match.save()  # recompute now that the sides exist

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
        sm = ScheduledMatchFactory(team1_players=[p1], team2_players=[p2])
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
        team1_players = kwargs.pop('team1_players', None)
        team2_players = kwargs.pop('team2_players', None)

        side1 = team1_players or [player1 or PlayerFactory(with_user=True)]
        side2 = team2_players or [player2 or PlayerFactory(with_user=True)]

        scheduled = super()._create(model_class, *args, **kwargs)
        set_scheduled_match_sides(scheduled, side1, side2)
        scheduled.save()
        return scheduled


class ChampionshipFactory(DjangoModelFactory):
    """Factory for Championship.

    Usage:
        # Basic singles championship
        champ = ChampionshipFactory()

        # With participants
        champ = ChampionshipFactory(with_participants=[team1, team2, team3])

        # Private championship
        champ = ChampionshipFactory(is_public=False)
    """
    class Meta:
        model = Championship

    name = factory.Sequence(lambda n: f"Championship {n}")
    championship_type = Championship.ChampionshipType.SINGLES
    is_public = True
    max_participants = 8
    start_date = factory.LazyFunction(lambda: date.today() + timedelta(days=14))
    registration_deadline = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    status = Championship.Status.REGISTRATION

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        participants = kwargs.pop('with_participants', None)
        entries = kwargs.pop('with_entries', None)
        created_by = kwargs.pop('created_by', None)

        if created_by:
            kwargs['created_by'] = created_by

        championship = super()._create(model_class, *args, **kwargs)

        # with_participants took Teams; it now takes lists of players, same as
        # with_entries.
        if participants:
            entries = entries or list(participants)

        for players in entries or []:
            entry = ChampionshipEntry.objects.create(championship=championship)
            ChampionshipEntryMember.objects.bulk_create([
                ChampionshipEntryMember(
                    entry=entry, player=p, championship=championship
                )
                for p in players
            ])

        return championship


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def create_match(side1_players, side2_players, **kwargs):
    """Create a Match with the given sides. For raw-ORM style tests."""
    match = Match.objects.create(**kwargs)
    set_match_sides(match, side1_players, side2_players)
    match.save()
    return match


def get_match_players(match):
    """Get (player1, player2) tuple for singles matches.

    Returns the first player from each team.
    For doubles, returns (team1 first player, team2 first player).
    """
    return match.side1_players.first(), match.side2_players.first()


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
        players = list(match.all_players)

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
        players = list(match.all_players)

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
    side = Side.ONE if team_num == 1 else Side.TWO
    return confirm_match(match, players=list(match.players_on(side)))


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

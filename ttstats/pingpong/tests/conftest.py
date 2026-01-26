import factory
import pytest
from datetime import date, time, timedelta
from django.contrib.auth.models import User, AnonymousUser
from django.test import Client
from factory.django import DjangoModelFactory

from pingpong.models import Game, Location, Match, Player, ScheduledMatch


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
    class Meta:
        model = Match

    player1 = factory.SubFactory(PlayerFactory, with_user=True)
    player2 = factory.SubFactory(PlayerFactory, with_user=True)
    best_of = 5
    match_type = "casual"


class GameFactory(DjangoModelFactory):
    class Meta:
        model = Game

    match = factory.SubFactory(MatchFactory)
    game_number = factory.Sequence(lambda n: n + 1)
    player1_score = 11
    player2_score = 5


class ScheduledMatchFactory(DjangoModelFactory):
    class Meta:
        model = ScheduledMatch

    player1 = factory.SubFactory(PlayerFactory, with_user=True)
    player2 = factory.SubFactory(PlayerFactory, with_user=True)
    scheduled_date = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    scheduled_time = factory.LazyFunction(lambda: time(14, 0))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    """A match with 3 games where player1 wins (best of 5)."""
    m = MatchFactory()
    GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
    GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
    GameFactory(match=m, game_number=3, player1_score=11, player2_score=7)
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

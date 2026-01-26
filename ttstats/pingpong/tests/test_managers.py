import pytest
from django.contrib.auth.models import AnonymousUser

from pingpong.models import Game, Match, Player, ScheduledMatch
from ttstats.middleware import _thread_locals
from .conftest import (
    GameFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
)


def _set_current_user(user):
    """Set thread-local user for manager filtering."""
    _thread_locals.user = user


def _clear_current_user():
    """Clear thread-local user."""
    if hasattr(_thread_locals, "user"):
        del _thread_locals.user


@pytest.fixture(autouse=True)
def clean_thread_locals():
    """Ensure thread-local is clean before and after each test."""
    _clear_current_user()
    yield
    _clear_current_user()


# ===========================================================================
# MatchManager
# ===========================================================================

@pytest.mark.django_db
class TestMatchManager:
    def test_no_user_context_returns_all(self):
        MatchFactory()
        MatchFactory()
        assert Match.objects.count() == 2

    def test_anonymous_user_returns_empty(self):
        MatchFactory()
        _set_current_user(AnonymousUser())
        assert Match.objects.count() == 0

    def test_staff_sees_all(self):
        MatchFactory()
        MatchFactory()
        staff = UserFactory(is_staff=True)
        _set_current_user(staff)
        assert Match.objects.count() == 2

    def test_regular_user_sees_own_matches(self):
        u = UserFactory()
        p = PlayerFactory(user=u)
        other_p = PlayerFactory(with_user=True)

        # Match where user is player1
        m1 = MatchFactory(player1=p, player2=other_p)
        # Match where user is player2
        m2 = MatchFactory(player1=other_p, player2=p)
        # Match user is not in
        m3 = MatchFactory()

        _set_current_user(u)
        visible = set(Match.objects.values_list("pk", flat=True))
        assert m1.pk in visible
        assert m2.pk in visible
        assert m3.pk not in visible

    def test_user_without_player_sees_empty(self):
        u = UserFactory()
        # Don't create a player for this user
        MatchFactory()
        _set_current_user(u)
        assert Match.objects.count() == 0


# ===========================================================================
# PlayerManager
# ===========================================================================

@pytest.mark.django_db
class TestPlayerManager:
    def test_get_queryset_returns_all(self):
        PlayerFactory()
        PlayerFactory()
        assert Player.objects.count() == 2

    def test_editable_by_staff(self):
        PlayerFactory()
        PlayerFactory()
        staff = UserFactory(is_staff=True)
        assert Player.objects.editable_by(staff).count() == 2

    def test_editable_by_regular_user(self):
        u = UserFactory()
        own = PlayerFactory(user=u)
        other = PlayerFactory(with_user=True)
        editable = Player.objects.editable_by(u)
        assert own in editable
        assert other not in editable

    def test_editable_by_anonymous(self):
        PlayerFactory()
        assert Player.objects.editable_by(AnonymousUser()).count() == 0

    def test_editable_by_none(self):
        PlayerFactory()
        assert Player.objects.editable_by(None).count() == 0


# ===========================================================================
# GameManager
# ===========================================================================

@pytest.mark.django_db
class TestGameManager:
    def test_no_user_context_returns_all(self):
        m = MatchFactory()
        GameFactory(match=m, game_number=1)
        assert Game.objects.count() == 1

    def test_anonymous_sees_none(self):
        m = MatchFactory()
        GameFactory(match=m, game_number=1)
        _set_current_user(AnonymousUser())
        assert Game.objects.count() == 0

    def test_staff_sees_all(self):
        m = MatchFactory()
        GameFactory(match=m, game_number=1)
        staff = UserFactory(is_staff=True)
        _set_current_user(staff)
        assert Game.objects.count() == 1

    def test_regular_user_sees_own_match_games(self):
        u = UserFactory()
        p = PlayerFactory(user=u)
        other_p = PlayerFactory(with_user=True)

        my_match = MatchFactory(player1=p, player2=other_p)
        GameFactory(match=my_match, game_number=1)

        other_match = MatchFactory()
        GameFactory(match=other_match, game_number=1)

        _set_current_user(u)
        assert Game.objects.count() == 1

    def test_user_without_player_sees_none(self):
        u = UserFactory()
        m = MatchFactory()
        GameFactory(match=m, game_number=1)
        _set_current_user(u)
        assert Game.objects.count() == 0


# ===========================================================================
# ScheduledMatchManager
# ===========================================================================

@pytest.mark.django_db
class TestScheduledMatchManager:
    def test_no_user_context_returns_all(self):
        ScheduledMatchFactory()
        ScheduledMatchFactory()
        assert ScheduledMatch.objects.count() == 2

    def test_anonymous_returns_empty(self):
        ScheduledMatchFactory()
        _set_current_user(AnonymousUser())
        assert ScheduledMatch.objects.count() == 0

    def test_staff_sees_all(self):
        ScheduledMatchFactory()
        ScheduledMatchFactory()
        staff = UserFactory(is_staff=True)
        _set_current_user(staff)
        assert ScheduledMatch.objects.count() == 2

    def test_regular_user_sees_own(self):
        u = UserFactory()
        p = PlayerFactory(user=u)
        other_p = PlayerFactory(with_user=True)

        sm1 = ScheduledMatchFactory(player1=p, player2=other_p)
        sm2 = ScheduledMatchFactory()  # not involved

        _set_current_user(u)
        visible = set(ScheduledMatch.objects.values_list("pk", flat=True))
        assert sm1.pk in visible
        assert sm2.pk not in visible

    def test_user_without_player_sees_empty(self):
        u = UserFactory()
        ScheduledMatchFactory()
        _set_current_user(u)
        assert ScheduledMatch.objects.count() == 0

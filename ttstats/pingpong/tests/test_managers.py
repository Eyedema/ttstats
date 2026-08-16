import pytest
from django.contrib.auth.models import AnonymousUser

from pingpong.models import Championship, Game, Match, Player, ScheduledMatch
from ttstats.middleware import _thread_locals
from .conftest import (
    ChampionshipFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
)


def _user_with_player():
    u = UserFactory()
    return u, PlayerFactory(user=u)


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

    def test_doubles_match_appears_exactly_once(self):
        """The OR across two M2M joins cross-products rows; only .distinct()
        collapses them. Any rewrite must keep the row count at one.
        """
        u, p = _user_with_player()
        partner = PlayerFactory(with_user=True)
        opp1 = PlayerFactory(with_user=True)
        opp2 = PlayerFactory(with_user=True)
        m = MatchFactory(
            team1_players=[p, partner], team2_players=[opp1, opp2], is_double=True
        )

        _set_current_user(u)
        rows = list(Match.objects.all())
        assert [row.pk for row in rows] == [m.pk]
        assert Match.objects.count() == 1

    def test_championship_participant_sees_matches_they_are_not_in(self):
        """A championship entrant can see every match in that championship,
        not only their own -- the Exists(championship_qs) branch.
        """
        u, p = _user_with_player()
        champ = ChampionshipFactory(with_entries=[[p]])

        others = [PlayerFactory(with_user=True) for _ in range(2)]
        champ_match = MatchFactory(
            player1=others[0], player2=others[1], championship=champ
        )
        unrelated = MatchFactory()

        _set_current_user(u)
        visible = set(Match.objects.values_list("pk", flat=True))
        assert champ_match.pk in visible
        assert unrelated.pk not in visible

    def test_live_matches_are_hidden(self):
        u, p = _user_with_player()
        other = PlayerFactory(with_user=True)
        live = MatchFactory(player1=p, player2=other, is_live=True)
        done = MatchFactory(player1=p, player2=other)

        _set_current_user(u)
        visible = set(Match.objects.values_list("pk", flat=True))
        assert done.pk in visible
        assert live.pk not in visible
        assert live.pk in set(Match.all_objects.values_list("pk", flat=True))


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

    def test_doubles_scheduled_match_appears_exactly_once(self):
        u, p = _user_with_player()
        partner = PlayerFactory(with_user=True)
        opp1 = PlayerFactory(with_user=True)
        opp2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(
            team1_players=[p, partner], team2_players=[opp1, opp2]
        )

        _set_current_user(u)
        assert [row.pk for row in ScheduledMatch.objects.all()] == [sm.pk]

    def test_championship_participant_sees_scheduled_matches_they_are_not_in(self):
        u, p = _user_with_player()
        champ = ChampionshipFactory(with_entries=[[p]])

        others = [PlayerFactory(with_user=True) for _ in range(2)]
        champ_sm = ScheduledMatchFactory(
            player1=others[0], player2=others[1], championship=champ
        )
        unrelated = ScheduledMatchFactory()

        _set_current_user(u)
        visible = set(ScheduledMatch.objects.values_list("pk", flat=True))
        assert champ_sm.pk in visible
        assert unrelated.pk not in visible


# ===========================================================================
# ChampionshipManager
# ===========================================================================

@pytest.mark.django_db
class TestChampionshipManager:
    def test_no_user_context_returns_all(self):
        ChampionshipFactory()
        ChampionshipFactory(is_public=False)
        assert Championship.objects.count() == 2

    def test_anonymous_returns_empty(self):
        ChampionshipFactory()
        _set_current_user(AnonymousUser())
        assert Championship.objects.count() == 0

    def test_staff_sees_all(self):
        ChampionshipFactory()
        ChampionshipFactory(is_public=False)
        staff = UserFactory(is_staff=True)
        _set_current_user(staff)
        assert Championship.objects.count() == 2

    def test_regular_user_sees_public_only_by_default(self):
        u, _ = _user_with_player()
        public = ChampionshipFactory()
        private = ChampionshipFactory(is_public=False)

        _set_current_user(u)
        visible = set(Championship.objects.values_list("pk", flat=True))
        assert public.pk in visible
        assert private.pk not in visible

    def test_regular_user_sees_private_championship_they_entered(self):
        u, p = _user_with_player()
        private = ChampionshipFactory(
            is_public=False, with_participants=[[p]]
        )

        _set_current_user(u)
        assert private.pk in set(Championship.objects.values_list("pk", flat=True))

    def test_regular_user_sees_private_championship_they_created(self):
        u, p = _user_with_player()
        private = ChampionshipFactory(is_public=False, created_by=p)

        _set_current_user(u)
        assert private.pk in set(Championship.objects.values_list("pk", flat=True))

    def test_user_without_player_sees_public_only(self):
        u = UserFactory()
        public = ChampionshipFactory()
        private = ChampionshipFactory(is_public=False)

        _set_current_user(u)
        visible = set(Championship.objects.values_list("pk", flat=True))
        assert visible == {public.pk}

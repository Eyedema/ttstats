import pytest

from pingpong.models import Team
from pingpong.services import resolve_sides, resolve_team
from .conftest import PlayerFactory, TeamFactory


@pytest.mark.django_db
class TestResolveTeam:
    def test_creates_a_singleton_team_when_none_exists(self):
        p = PlayerFactory()
        team = resolve_team([p])
        assert list(team.players.all()) == [p]

    def test_reuses_the_existing_singleton_team(self):
        p = PlayerFactory()
        first = resolve_team([p])
        second = resolve_team([p])
        assert first.pk == second.pk
        assert Team.objects.count() == 1

    def test_reuses_a_doubles_team_regardless_of_order(self):
        a = PlayerFactory()
        b = PlayerFactory()
        first = resolve_team([a, b])
        second = resolve_team([b, a])
        assert first.pk == second.pk
        assert Team.objects.count() == 1

    def test_does_not_reuse_a_larger_team_that_contains_the_pair(self):
        """MatchCreateView's doubles branch used to match on membership alone,
        so a three-player team containing both players would be reused.
        """
        a = PlayerFactory()
        b = PlayerFactory()
        c = PlayerFactory()
        oversized = TeamFactory(players=[a, b, c])

        team = resolve_team([a, b])

        assert team.pk != oversized.pk
        assert set(team.players.all()) == {a, b}

    def test_does_not_reuse_a_singleton_team_for_a_pair(self):
        a = PlayerFactory()
        b = PlayerFactory()
        singleton = resolve_team([a])

        pair = resolve_team([a, b])

        assert pair.pk != singleton.pk

    def test_does_not_reuse_a_pair_team_for_a_singleton(self):
        a = PlayerFactory()
        b = PlayerFactory()
        pair = resolve_team([a, b])

        singleton = resolve_team([a])

        assert singleton.pk != pair.pk
        assert list(singleton.players.all()) == [a]

    def test_empty_player_list_is_rejected(self):
        with pytest.raises(ValueError):
            resolve_team([])


@pytest.mark.django_db
class TestResolveSides:
    def test_returns_a_team_per_side(self):
        a = PlayerFactory()
        b = PlayerFactory()
        team1, team2 = resolve_sides([a], [b])
        assert list(team1.players.all()) == [a]
        assert list(team2.players.all()) == [b]
        assert team1.pk != team2.pk

    def test_doubles_sides(self):
        players = [PlayerFactory() for _ in range(4)]
        team1, team2 = resolve_sides(players[:2], players[2:])
        assert set(team1.players.all()) == set(players[:2])
        assert set(team2.players.all()) == set(players[2:])

    def test_repeat_calls_do_not_multiply_teams(self):
        a = PlayerFactory()
        b = PlayerFactory()
        resolve_sides([a], [b])
        resolve_sides([a], [b])
        assert Team.objects.count() == 2

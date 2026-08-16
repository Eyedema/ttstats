import pytest

from pingpong.models import MatchParticipant, ScheduledMatchParticipant, Side
from pingpong.services import set_match_sides, set_scheduled_match_sides
from .conftest import MatchFactory, PlayerFactory, ScheduledMatchFactory


@pytest.mark.django_db
class TestSetMatchSides:
    def test_writes_one_participant_per_player(self):
        a, b = PlayerFactory(), PlayerFactory()
        match = MatchFactory()

        set_match_sides(match, [a], [b])

        assert list(match.players_on(Side.ONE)) == [a]
        assert list(match.players_on(Side.TWO)) == [b]
        assert match.participants.count() == 2

    def test_doubles_sides(self):
        players = [PlayerFactory() for _ in range(4)]
        match = MatchFactory()

        set_match_sides(match, players[:2], players[2:])

        assert set(match.players_on(Side.ONE)) == set(players[:2])
        assert set(match.players_on(Side.TWO)) == set(players[2:])

    def test_replaces_previous_sides(self):
        old_a, old_b = PlayerFactory(), PlayerFactory()
        new_a, new_b = PlayerFactory(), PlayerFactory()
        match = MatchFactory()

        set_match_sides(match, [old_a], [old_b])
        set_match_sides(match, [new_a], [new_b])

        assert match.participants.count() == 2
        assert list(match.players_on(Side.ONE)) == [new_a]
        assert list(match.players_on(Side.TWO)) == [new_b]

    def test_is_scoped_to_one_match(self):
        a, b = PlayerFactory(), PlayerFactory()
        mine = MatchFactory()
        other = MatchFactory()
        other_participants = set(
            other.participants.values_list("player_id", flat=True)
        )

        set_match_sides(mine, [a], [b])

        assert (
            set(other.participants.values_list("player_id", flat=True))
            == other_participants
        )

    def test_empty_sides_are_allowed(self):
        match = MatchFactory()
        set_match_sides(match, [], [])
        assert match.participants.count() == 0


@pytest.mark.django_db
class TestSetScheduledMatchSides:
    def test_writes_participants(self):
        a, b = PlayerFactory(), PlayerFactory()
        scheduled = ScheduledMatchFactory()

        set_scheduled_match_sides(scheduled, [a], [b])

        assert list(scheduled.players_on(Side.ONE)) == [a]
        assert list(scheduled.players_on(Side.TWO)) == [b]

    def test_does_not_touch_match_participants(self):
        a, b = PlayerFactory(), PlayerFactory()
        scheduled = ScheduledMatchFactory()
        before = MatchParticipant.objects.count()

        set_scheduled_match_sides(scheduled, [a], [b])

        assert MatchParticipant.objects.count() == before
        assert ScheduledMatchParticipant.objects.filter(
            scheduled_match=scheduled
        ).count() == 2

import pytest
from datetime import date, timedelta

from pingpong.models import Championship, ScheduledMatch, Match, Side
from .conftest import (
    ChampionshipFactory,
    GameFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    UserFactory,
    confirm_match,
)


def _singles_team(player):
    """Entries are per player now; kept so call sites read the same."""
    return [player]


def _make_participants(n=4):
    """Create n players each with a 1-player team. Returns (players, teams)."""
    players = [PlayerFactory(with_user=True) for _ in range(n)]
    teams = [[p] for p in players]
    return players, teams


def _entry_of(championship, player):
    """The championship entry containing this player."""
    return championship.entries.get(members__player=player)


# ---------------------------------------------------------------------------
# Championship Model
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipModel:
    def test_str(self):
        champ = ChampionshipFactory(name="Spring Cup")
        assert "Spring Cup" in str(champ)
        assert "Registration Open" in str(champ)

    def test_is_registration_open_public(self):
        champ = ChampionshipFactory(
            registration_deadline=date.today() + timedelta(days=3)
        )
        assert champ.is_registration_open is True

    def test_is_registration_open_past_deadline(self):
        champ = ChampionshipFactory(
            registration_deadline=date.today() - timedelta(days=1)
        )
        assert champ.is_registration_open is False

    def test_is_registration_open_private(self):
        champ = ChampionshipFactory(is_public=False)
        assert champ.is_registration_open is False

    def test_is_registration_open_wrong_status(self):
        champ = ChampionshipFactory(status=Championship.Status.IN_PROGRESS)
        assert champ.is_registration_open is False

    def test_current_participants_count(self):
        _, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        assert champ.current_participants_count == 3

    def test_is_full(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(max_participants=2, with_participants=teams)
        assert champ.is_full is True

    def test_is_not_full(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(max_participants=8, with_participants=teams)
        assert champ.is_full is False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipRegistration:
    def test_can_register_valid_entry(self):
        player = PlayerFactory(with_user=True)
        champ = ChampionshipFactory()
        assert champ.can_register([player]) is True

    def test_can_register_returns_false_when_full(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(max_participants=2, with_participants=teams)
        new_player = PlayerFactory(with_user=True)
        assert champ.can_register([new_player]) is False

    def test_can_register_returns_false_when_already_registered(self):
        player = PlayerFactory(with_user=True)
        team = _singles_team(player)
        champ = ChampionshipFactory(with_participants=[team])
        assert champ.can_register([player]) is False

    def test_can_register_returns_false_if_any_player_already_entered(self):
        """A player may not appear in two entries of the same championship."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        p3 = PlayerFactory(with_user=True)
        champ = ChampionshipFactory(
            championship_type=Championship.ChampionshipType.DOUBLES
        )
        assert champ.register_entry([p1, p2]) is not None
        assert champ.can_register([p2, p3]) is False

    def test_can_register_returns_false_for_wrong_entry_size(self):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        champ = ChampionshipFactory(championship_type=Championship.ChampionshipType.SINGLES)
        assert champ.can_register([p1, p2]) is False

    def test_can_register_doubles_correct_size(self):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        champ = ChampionshipFactory(championship_type=Championship.ChampionshipType.DOUBLES)
        assert champ.can_register([p1, p2]) is True

    def test_can_register_rejects_the_same_player_twice_in_one_entry(self):
        p1 = PlayerFactory(with_user=True)
        champ = ChampionshipFactory(championship_type=Championship.ChampionshipType.DOUBLES)
        assert champ.can_register([p1, p1]) is False

    def test_register_entry_success(self):
        player = PlayerFactory(with_user=True)
        champ = ChampionshipFactory()
        entry = champ.register_entry([player])
        assert entry is not None
        assert champ.entries.count() == 1
        assert list(entry.players) == [player]

    def test_register_entry_failure_when_full(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(max_participants=2, with_participants=teams)
        new_player = PlayerFactory(with_user=True)
        assert champ.register_entry([new_player]) is None


# ---------------------------------------------------------------------------
# Schedule Generation
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestGenerateSchedule:
    def test_generate_schedule_creates_correct_match_count(self):
        """n participants => n*(n-1) matches (home + away)."""
        _, teams = _make_participants(4)
        champ = ChampionshipFactory(with_participants=teams)
        result = champ.generate_schedule()
        assert result is True
        assert ScheduledMatch.all_objects.filter(championship=champ).count() == 12  # 4*3

    def test_generate_schedule_three_participants(self):
        """3 participants => 3*2 = 6 matches."""
        _, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        assert ScheduledMatch.all_objects.filter(championship=champ).count() == 6

    def test_generate_schedule_two_participants(self):
        """2 participants => 2 matches (home + away)."""
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        assert ScheduledMatch.all_objects.filter(championship=champ).count() == 2

    def test_generate_schedule_fails_with_less_than_two(self):
        _, teams = _make_participants(1)
        champ = ChampionshipFactory(with_participants=teams)
        assert champ.generate_schedule() is False

    def test_generate_schedule_sets_round_numbers(self):
        _, teams = _make_participants(4)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        matches = ScheduledMatch.all_objects.filter(championship=champ)
        assert all(m.round_number is not None for m in matches)

    def test_generate_schedule_weekly_spacing(self):
        """Rounds should be spaced 7 days apart."""
        _, teams = _make_participants(4)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        dates = (
            ScheduledMatch.all_objects.filter(championship=champ)
            .values_list('scheduled_date', flat=True)
            .distinct()
            .order_by('scheduled_date')
        )
        dates = list(dates)
        for i in range(1, len(dates)):
            assert (dates[i] - dates[i - 1]).days == 7

    def test_generate_schedule_home_and_away(self):
        """Each pair of entrants should play once as home and once as away."""
        players, _ = _make_participants(3)
        champ = ChampionshipFactory(with_entries=[[p] for p in players])
        champ.generate_schedule()
        matches = ScheduledMatch.all_objects.filter(championship=champ)

        def _played(home_player, away_player):
            return matches.filter(
                participants__player=home_player,
                participants__side=Side.ONE,
            ).filter(
                participants__player=away_player,
                participants__side=Side.TWO,
            ).count()

        for i, p1 in enumerate(players):
            for p2 in players[i + 1:]:
                assert _played(p1, p2) == 1, f"{p1} vs {p2} home games should be 1"
                assert _played(p2, p1) == 1, f"{p2} vs {p1} home games should be 1"

    def test_generate_schedule_sets_end_date(self):
        _, teams = _make_participants(4)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        champ.refresh_from_db()
        assert champ.end_date is not None
        assert champ.end_date > champ.start_date

    def test_generate_schedule_replaces_existing(self):
        """Calling generate_schedule again replaces old matches."""
        _, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        champ.generate_schedule()
        assert ScheduledMatch.all_objects.filter(championship=champ).count() == 6

    def test_generate_schedule_odd_participants(self):
        """5 participants => 5*4 = 20 matches, with bye handling."""
        _, teams = _make_participants(5)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        assert ScheduledMatch.all_objects.filter(championship=champ).count() == 20


# ---------------------------------------------------------------------------
# Standings
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestGetStandings:
    def test_standings_empty(self):
        _, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        standings = champ.get_standings()
        assert len(standings) == 3
        assert all(s['played'] == 0 for s in standings)
        assert all(s['points'] == 0 for s in standings)

    def test_standings_with_confirmed_match(self):
        players, teams = _make_participants(2)
        champ = ChampionshipFactory(with_participants=teams)

        # Create a confirmed match where team1 wins
        match = MatchFactory(
            team1_players=[players[0]], team2_players=[players[1]],
            championship=champ, match_type="tournament"
        )
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        confirm_match(match)

        standings = champ.get_standings()
        winner_standing = next(s for s in standings if s['entry'] == _entry_of(champ, players[0]))
        loser_standing = next(s for s in standings if s['entry'] == _entry_of(champ, players[1]))

        assert winner_standing['wins'] == 1
        assert winner_standing['points'] == 3
        assert winner_standing['games_won'] == 3
        assert loser_standing['losses'] == 1
        assert loser_standing['points'] == 0

    def test_standings_sorted_by_points(self):
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)

        # Team 0 beats Team 1
        m1 = MatchFactory(team1_players=[players[0]], team2_players=[players[1]], championship=champ)
        GameFactory(match=m1, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m1, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m1, game_number=3, team1_score=11, team2_score=9)
        m1.refresh_from_db()
        confirm_match(m1)

        # Team 2 beats Team 0
        m2 = MatchFactory(team1_players=[players[2]], team2_players=[players[0]], championship=champ)
        GameFactory(match=m2, game_number=1, team1_score=11, team2_score=3)
        GameFactory(match=m2, game_number=2, team1_score=11, team2_score=4)
        GameFactory(match=m2, game_number=3, team1_score=11, team2_score=6)
        m2.refresh_from_db()
        confirm_match(m2)

        # Team 2 beats Team 1
        m3 = MatchFactory(team1_players=[players[2]], team2_players=[players[1]], championship=champ)
        GameFactory(match=m3, game_number=1, team1_score=11, team2_score=2)
        GameFactory(match=m3, game_number=2, team1_score=11, team2_score=3)
        GameFactory(match=m3, game_number=3, team1_score=11, team2_score=1)
        m3.refresh_from_db()
        confirm_match(m3)

        standings = champ.get_standings()
        # Team 2 should be first (2 wins = 6 pts), Team 0 second (1 win = 3 pts)
        assert standings[0]['entry'] == _entry_of(champ, players[2])
        assert standings[0]['points'] == 6
        assert standings[1]['entry'] == _entry_of(champ, players[0])
        assert standings[1]['points'] == 3
        assert standings[2]['entry'] == _entry_of(champ, players[1])
        assert standings[2]['points'] == 0


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipPermissions:
    def test_user_can_view_public(self):
        champ = ChampionshipFactory(is_public=True)
        user = UserFactory()
        assert champ.user_can_view(user) is True

    def test_user_can_view_private_non_participant(self):
        champ = ChampionshipFactory(is_public=False)
        user = UserFactory()
        PlayerFactory(user=user)
        assert champ.user_can_view(user) is False

    def test_user_can_view_private_participant(self):
        player = PlayerFactory(with_user=True)
        team = _singles_team(player)
        champ = ChampionshipFactory(is_public=False, with_participants=[team])
        assert champ.user_can_view(player.user) is True

    def test_user_can_view_staff(self):
        champ = ChampionshipFactory(is_public=False)
        staff = UserFactory(is_staff=True)
        assert champ.user_can_view(staff) is True

    def test_user_can_view_anonymous(self):
        from django.contrib.auth.models import AnonymousUser
        champ = ChampionshipFactory()
        assert champ.user_can_view(AnonymousUser()) is False

    def test_user_can_edit_creator(self):
        player = PlayerFactory(with_user=True)
        champ = ChampionshipFactory(created_by=player)
        assert champ.user_can_edit(player.user) is True

    def test_user_can_edit_non_creator(self):
        creator = PlayerFactory(with_user=True)
        other = PlayerFactory(with_user=True)
        champ = ChampionshipFactory(created_by=creator)
        assert champ.user_can_edit(other.user) is False

    def test_user_can_edit_staff(self):
        champ = ChampionshipFactory()
        staff = UserFactory(is_staff=True)
        assert champ.user_can_edit(staff) is True


# ---------------------------------------------------------------------------
# check_completion
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCheckCompletion:
    def test_check_completion_returns_false_when_not_in_progress(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(status=Championship.Status.SCHEDULED, with_participants=teams)
        assert champ.check_completion() is False

    def test_check_completion_returns_false_when_matches_not_converted(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(status=Championship.Status.IN_PROGRESS, with_participants=teams)
        champ.generate_schedule()
        assert champ.check_completion() is False

    def test_check_completion_succeeds_when_all_confirmed(self):
        players, teams = _make_participants(2)
        champ = ChampionshipFactory(status=Championship.Status.IN_PROGRESS, with_participants=teams)
        champ.generate_schedule()

        # Convert and complete all scheduled matches
        for sm in ScheduledMatch.all_objects.filter(championship=champ):
            match = MatchFactory(
                team1_players=list(sm.side1_players),
                team2_players=list(sm.side2_players),
                championship=champ, match_type="tournament"
            )
            GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
            GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
            GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
            match.refresh_from_db()
            confirm_match(match)
            sm.match = match
            sm.save()

        assert champ.check_completion() is True
        champ.refresh_from_db()
        assert champ.status == Championship.Status.COMPLETED


@pytest.mark.django_db
class TestGeneratedScheduleParticipants:
    """generate_schedule uses bulk_create, which does not fire post_save --
    so the participant rows the save hook normally writes have to be built
    explicitly. Without this the generated matches have no participants.
    """

    def test_generated_scheduled_matches_have_participants(self):
        players, teams = _make_participants(4)
        champ = ChampionshipFactory(with_participants=teams)

        assert champ.generate_schedule() is True

        scheduled = ScheduledMatch.all_objects.filter(championship=champ)
        assert scheduled.exists()
        for sm in scheduled:
            assert sm.participants.count() == 2
            assert list(sm.side1_players) != []
            assert list(sm.side2_players) != []

    def test_generated_scheduled_matches_are_linked_to_entries(self):
        players, teams = _make_participants(4)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()

        for sm in ScheduledMatch.all_objects.filter(championship=champ):
            assert sm.side1_entry is not None
            assert sm.side2_entry is not None
            assert sm.side1_entry != sm.side2_entry
            assert set(sm.side1_players) == set(sm.side1_entry.players)

    def test_side_labels_are_not_empty(self):
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()

        for sm in ScheduledMatch.all_objects.filter(championship=champ):
            assert sm.side1_label != "Side 1"
            assert sm.side2_label != "Side 2"

import pytest
from datetime import date, timedelta

from pingpong.models import Championship, ScheduledMatch, Match
from .conftest import (
    ChampionshipFactory,
    GameFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    TeamFactory,
    UserFactory,
    confirm_match,
)


def _singles_team(player):
    """Create a 1-player team for a player."""
    return TeamFactory(players=[player])


def _make_participants(n=4):
    """Create n players each with a 1-player team. Returns (players, teams)."""
    players = [PlayerFactory(with_user=True) for _ in range(n)]
    teams = [_singles_team(p) for p in players]
    return players, teams


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
        champ = ChampionshipFactory(status="in_progress")
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
    def test_can_register_valid_team(self):
        player = PlayerFactory(with_user=True)
        team = _singles_team(player)
        champ = ChampionshipFactory()
        assert champ.can_register(team) is True

    def test_can_register_returns_false_when_full(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(max_participants=2, with_participants=teams)
        new_player = PlayerFactory(with_user=True)
        new_team = _singles_team(new_player)
        assert champ.can_register(new_team) is False

    def test_can_register_returns_false_when_already_registered(self):
        player = PlayerFactory(with_user=True)
        team = _singles_team(player)
        champ = ChampionshipFactory(with_participants=[team])
        assert champ.can_register(team) is False

    def test_can_register_returns_false_for_wrong_team_size(self):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        doubles_team = TeamFactory(players=[p1, p2])
        champ = ChampionshipFactory(championship_type="singles")
        assert champ.can_register(doubles_team) is False

    def test_can_register_doubles_correct_size(self):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        doubles_team = TeamFactory(players=[p1, p2])
        champ = ChampionshipFactory(championship_type="doubles")
        assert champ.can_register(doubles_team) is True

    def test_register_team_success(self):
        player = PlayerFactory(with_user=True)
        team = _singles_team(player)
        champ = ChampionshipFactory()
        assert champ.register_team(team) is True
        assert champ.participants.filter(pk=team.pk).exists()

    def test_register_team_failure(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(max_participants=2, with_participants=teams)
        new_player = PlayerFactory(with_user=True)
        new_team = _singles_team(new_player)
        assert champ.register_team(new_team) is False


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
        """Each pair of teams should play once as home and once as away."""
        _, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        champ.generate_schedule()
        matches = ScheduledMatch.all_objects.filter(championship=champ)

        for i, t1 in enumerate(teams):
            for t2 in teams[i + 1:]:
                home = matches.filter(team1=t1, team2=t2).count()
                away = matches.filter(team1=t2, team2=t1).count()
                assert home == 1, f"{t1} vs {t2} home games should be 1, got {home}"
                assert away == 1, f"{t2} vs {t1} away games should be 1, got {away}"

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
            team1=teams[0], team2=teams[1],
            championship=champ, match_type="tournament"
        )
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        confirm_match(match)

        standings = champ.get_standings()
        winner_standing = next(s for s in standings if s['team'] == teams[0])
        loser_standing = next(s for s in standings if s['team'] == teams[1])

        assert winner_standing['wins'] == 1
        assert winner_standing['points'] == 3
        assert winner_standing['games_won'] == 3
        assert loser_standing['losses'] == 1
        assert loser_standing['points'] == 0

    def test_standings_sorted_by_points(self):
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)

        # Team 0 beats Team 1
        m1 = MatchFactory(team1=teams[0], team2=teams[1], championship=champ)
        GameFactory(match=m1, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m1, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m1, game_number=3, team1_score=11, team2_score=9)
        m1.refresh_from_db()
        confirm_match(m1)

        # Team 2 beats Team 0
        m2 = MatchFactory(team1=teams[2], team2=teams[0], championship=champ)
        GameFactory(match=m2, game_number=1, team1_score=11, team2_score=3)
        GameFactory(match=m2, game_number=2, team1_score=11, team2_score=4)
        GameFactory(match=m2, game_number=3, team1_score=11, team2_score=6)
        m2.refresh_from_db()
        confirm_match(m2)

        # Team 2 beats Team 1
        m3 = MatchFactory(team1=teams[2], team2=teams[1], championship=champ)
        GameFactory(match=m3, game_number=1, team1_score=11, team2_score=2)
        GameFactory(match=m3, game_number=2, team1_score=11, team2_score=3)
        GameFactory(match=m3, game_number=3, team1_score=11, team2_score=1)
        m3.refresh_from_db()
        confirm_match(m3)

        standings = champ.get_standings()
        # Team 2 should be first (2 wins = 6 pts), Team 0 second (1 win = 3 pts)
        assert standings[0]['team'] == teams[2]
        assert standings[0]['points'] == 6
        assert standings[1]['team'] == teams[0]
        assert standings[1]['points'] == 3
        assert standings[2]['team'] == teams[1]
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
        champ = ChampionshipFactory(status="scheduled", with_participants=teams)
        assert champ.check_completion() is False

    def test_check_completion_returns_false_when_matches_not_converted(self):
        _, teams = _make_participants(2)
        champ = ChampionshipFactory(status="in_progress", with_participants=teams)
        champ.generate_schedule()
        assert champ.check_completion() is False

    def test_check_completion_succeeds_when_all_confirmed(self):
        players, teams = _make_participants(2)
        champ = ChampionshipFactory(status="in_progress", with_participants=teams)
        champ.generate_schedule()

        # Convert and complete all scheduled matches
        for sm in ScheduledMatch.all_objects.filter(championship=champ):
            match = MatchFactory(
                team1=sm.team1, team2=sm.team2,
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
        assert champ.status == "completed"

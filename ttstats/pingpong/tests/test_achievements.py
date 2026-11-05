from datetime import date, datetime, time, timezone as dt_timezone

import pytest

from pingpong.achievements import (
    check_achievements_for_player,
    check_comeback_king,
    check_deuce_master,
    check_first_blood,
    check_giant_slayer,
    check_iron_wall,
    check_marathon_match,
    check_matches_played,
    check_matches_won,
    check_peak_performer,
    check_perfect_game,
    check_rivalry,
    check_win_streak,
    get_achievement_progress,
)
from pingpong.achievement_definitions import ACHIEVEMENT_DEFINITIONS
from pingpong.models import Achievement, PlayerAchievement, RatingChange

from .conftest import (
    GameFactory,
    MatchFactory,
    PlayerFactory,
    PlayerRatingFactory,
    SeasonFactory,
    confirm_match,
    confirm_match_silent,
)


# ---------------------------------------------------------------------------
# Fixture to seed achievements
# ---------------------------------------------------------------------------

@pytest.fixture
def achievements(db):
    for defn in ACHIEVEMENT_DEFINITIONS:
        Achievement.objects.get_or_create(slug=defn['slug'], defaults=defn)


def _make_confirmed_match(p1, p2, scores, best_of=5):
    """Helper: create a confirmed match with given game scores.

    scores: list of (team1_score, team2_score) tuples
    Returns the match (refreshed from DB).
    """
    m = MatchFactory(player1=p1, player2=p2, best_of=best_of)
    for i, (s1, s2) in enumerate(scores, start=1):
        GameFactory(match=m, game_number=i, team1_score=s1, team2_score=s2)
    m.refresh_from_db()
    # Use silent confirm to avoid triggering signals (we test checkers directly)
    confirm_match_silent(m)
    m.refresh_from_db()
    return m


def _rated(player, glicko_rating, fmt="singles"):
    """An all-time rated PlayerRating row the next match is projected from."""
    return PlayerRatingFactory(
        player=player, format=fmt, glicko_rating=glicko_rating, rd=60.0,
        volatility=0.06, last_rated_on=date(2026, 1, 1), matches=30,
    )


def _all_time_change(match, player):
    return RatingChange.objects.get(match=match, player=player, rating__season__isnull=True)


def _set_change(match, player, **glicko):
    """Pin a player's all-time Glicko movement for `match` to exact values."""
    RatingChange.objects.filter(
        match=match, player=player, rating__season__isnull=True
    ).update(**glicko)


def _win(p1, p2):
    return _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])


# ===========================================================================
# Model tests
# ===========================================================================

@pytest.mark.django_db
class TestAchievementModel:
    def test_str_one_off(self, achievements):
        ach = Achievement.objects.get(slug='first_blood')
        assert str(ach) == 'First Blood'

    def test_str_tiered(self, achievements):
        ach = Achievement.objects.get(slug='win_streak_gold')
        assert str(ach) == 'Win Streak (Gold)'

    def test_total_count(self, achievements):
        assert Achievement.objects.count() == len(ACHIEVEMENT_DEFINITIONS)


@pytest.mark.django_db
class TestPlayerAchievementModel:
    def test_unique_together_prevents_duplicates(self, achievements):
        p = PlayerFactory()
        ach = Achievement.objects.get(slug='first_blood')
        PlayerAchievement.objects.create(player=p, achievement=ach)
        with pytest.raises(Exception):
            PlayerAchievement.objects.create(player=p, achievement=ach)


# ===========================================================================
# Checker tests
# ===========================================================================

@pytest.mark.django_db
class TestFirstBlood:
    def test_awarded_on_first_win(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        assert 'first_blood' in check_first_blood(p1, m)

    def test_not_awarded_on_loss(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(5, 11), (5, 11), (5, 11)])
        assert check_first_blood(p1, m) == []

    def test_still_returns_slug_on_subsequent_wins(self, achievements):
        """The checker returns the slug; dedup happens in check_achievements_for_player."""
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        m2 = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        assert 'first_blood' in check_first_blood(p1, m2)


@pytest.mark.django_db
class TestMatchesPlayed:
    def test_bronze_at_10(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(10):
            _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        # 11 total now
        result = check_matches_played(p1, m)
        assert 'matches_played_bronze' in result

    def test_not_awarded_below_10(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(8):
            _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        result = check_matches_played(p1, m)
        assert 'matches_played_bronze' not in result


@pytest.mark.django_db
class TestMatchesWon:
    def test_bronze_at_10(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(10):
            _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        result = check_matches_won(p1, m)
        assert 'matches_won_bronze' in result

    def test_losses_dont_count(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(10):
            _make_confirmed_match(p1, p2, [(5, 11), (5, 11), (5, 11)])
        m = _make_confirmed_match(p1, p2, [(5, 11), (5, 11), (5, 11)])
        result = check_matches_won(p1, m)
        assert result == []


@pytest.mark.django_db
class TestWinStreak:
    def test_bronze_at_3(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(3):
            m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        result = check_win_streak(p1, m)
        assert 'win_streak_bronze' in result

    def test_streak_resets_on_loss(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        _make_confirmed_match(p1, p2, [(5, 11), (5, 11), (5, 11)])  # loss
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        result = check_win_streak(p1, m)
        assert 'win_streak_bronze' not in result  # only 1 after loss


@pytest.mark.django_db
class TestPerfectGame:
    def test_awarded_for_11_0(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(11, 0), (11, 5), (11, 5)])
        result = check_perfect_game(p1, m)
        assert 'perfect_game' in result

    def test_not_awarded_for_11_1(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(11, 1), (11, 5), (11, 5)])
        result = check_perfect_game(p1, m)
        assert result == []

    def test_awarded_for_0_11_loss_game_if_opponent(self, achievements):
        """The losing side can also earn it if they won a game 11-0."""
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        # p2 wins game 2 as 0-11 (team2_score=11, team1_score=0)
        m = _make_confirmed_match(p1, p2, [(11, 5), (0, 11), (11, 5), (11, 5)])
        result = check_perfect_game(p2, m)
        assert 'perfect_game' in result


@pytest.mark.django_db
class TestComebackKing:
    def test_awarded_for_0_2_then_win(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(
            p1, p2,
            [(5, 11), (5, 11), (11, 5), (11, 5), (11, 5)],
            best_of=5,
        )
        result = check_comeback_king(p1, m)
        assert 'comeback_king' in result

    def test_not_awarded_in_best_of_3(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(
            p1, p2,
            [(5, 11), (11, 5), (11, 5)],
            best_of=3,
        )
        result = check_comeback_king(p1, m)
        assert result == []

    def test_not_awarded_for_3_1_win(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(
            p1, p2,
            [(5, 11), (11, 5), (11, 5), (11, 5)],
            best_of=5,
        )
        result = check_comeback_king(p1, m)
        assert result == []

    def test_not_awarded_on_loss(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(
            p1, p2,
            [(11, 5), (11, 5), (5, 11), (5, 11), (5, 11)],
            best_of=5,
        )
        result = check_comeback_king(p1, m)
        assert result == []


@pytest.mark.django_db
class TestIronWall:
    def test_awarded_under_20_points(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        # 5 + 6 + 7 = 18 < 20
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 6), (11, 7)])
        result = check_iron_wall(p1, m)
        assert 'iron_wall' in result

    def test_not_awarded_at_20_points(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        # 7 + 6 + 7 = 20 (not < 20)
        m = _make_confirmed_match(p1, p2, [(11, 7), (11, 6), (11, 7)])
        result = check_iron_wall(p1, m)
        assert result == []

    def test_not_awarded_on_loss(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(5, 11), (5, 11), (5, 11)])
        result = check_iron_wall(p1, m)
        assert result == []


@pytest.mark.django_db
class TestMarathonMatch:
    def test_awarded_for_3_2_in_bo5(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(
            p1, p2,
            [(11, 5), (5, 11), (11, 5), (5, 11), (11, 5)],
            best_of=5,
        )
        result = check_marathon_match(p1, m)
        assert 'marathon_match' in result

    def test_not_awarded_for_3_0(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        result = check_marathon_match(p1, m)
        assert result == []

    def test_awarded_for_2_1_in_bo3(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(
            p1, p2,
            [(11, 5), (5, 11), (11, 5)],
            best_of=3,
        )
        result = check_marathon_match(p1, m)
        assert 'marathon_match' in result


@pytest.mark.django_db
class TestDeuceMaster:
    def test_bronze_at_3_deuce_wins(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        # 3 deuce games won by p1 across multiple matches
        _make_confirmed_match(p1, p2, [(12, 10), (12, 10), (11, 5)])
        m = _make_confirmed_match(p1, p2, [(12, 10), (11, 5), (11, 5)])
        result = check_deuce_master(p1, m)
        assert 'deuce_master_bronze' in result

    def test_9_11_is_not_deuce(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        # 9-11 is not deuce (both must be >= 10)
        m = _make_confirmed_match(p1, p2, [(11, 9), (11, 9), (11, 9)])
        result = check_deuce_master(p1, m)
        assert result == []


@pytest.mark.django_db
class TestGiantSlayer:
    """Gap = opponents' mean pre-match all-time Glicko rating minus mine."""

    def test_reads_the_pre_match_glicko_ratings_from_the_pipeline(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        _rated(p1, 1400.0)
        _rated(p2, 1600.0)  # 200 higher
        m = _win(p1, p2)
        assert _all_time_change(m, p1).glicko_old_rating == pytest.approx(1400.0)
        assert _all_time_change(m, p2).glicko_old_rating == pytest.approx(1600.0)

        result = check_giant_slayer(p1, m)

        assert result == ['giant_slayer_bronze', 'giant_slayer_silver']

    @pytest.mark.parametrize("gap, expected", [
        (99.9, []),
        (100, ['giant_slayer_bronze']),
        (199.9, ['giant_slayer_bronze']),
        (200, ['giant_slayer_bronze', 'giant_slayer_silver']),
        (299.9, ['giant_slayer_bronze', 'giant_slayer_silver']),
        (300, ['giant_slayer_bronze', 'giant_slayer_silver', 'giant_slayer_gold']),
    ])
    def test_thresholds_are_100_200_300(self, achievements, gap, expected):
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=1400.0)
        _set_change(m, p2, glicko_old_rating=1400.0 + gap)

        assert check_giant_slayer(p1, m) == expected

    def test_not_awarded_when_higher_rated(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        _rated(p1, 1600.0)
        _rated(p2, 1400.0)
        m = _win(p1, p2)
        assert check_giant_slayer(p1, m) == []

    def test_not_awarded_to_the_loser(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=1800.0)
        _set_change(m, p2, glicko_old_rating=1400.0)
        assert check_giant_slayer(p2, m) == []

    def test_doubles_compare_against_the_opponents_mean(self, achievements):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        m = MatchFactory(team1_players=[a, b], team2_players=[c, d], is_double=True, best_of=3)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=5)
        m.refresh_from_db()
        _set_change(m, a, glicko_old_rating=1400.0)
        _set_change(m, c, glicko_old_rating=1450.0)
        _set_change(m, d, glicko_old_rating=1550.0)  # mean 1500: a gap of 100

        assert check_giant_slayer(a, m) == ['giant_slayer_bronze']

    def test_a_skipped_projection_is_not_a_win_over_anyone(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=1000.0)
        _set_change(m, p2, glicko_old_rating=None, glicko_new_rating=None)
        assert check_giant_slayer(p1, m) == []

    def test_season_rows_are_ignored(self, achievements):
        # Achievements are a career record: only the all-time rows count.
        season = SeasonFactory()
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = MatchFactory(
            player1=p1, player2=p2, best_of=3, club=season.club,
            date_played=datetime.combine(season.starts_on, time(12), tzinfo=dt_timezone.utc),
        )
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=5)
        m.refresh_from_db()
        RatingChange.objects.filter(match=m, player=p2, rating__season=season).update(
            glicko_old_rating=1900.0
        )
        assert RatingChange.objects.filter(match=m, rating__season=season).count() == 2
        assert check_giant_slayer(p1, m) == []


@pytest.mark.django_db
class TestRivalry:
    def test_bronze_at_5_matches_same_opponent(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(5):
            m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        result = check_rivalry(p1, m)
        assert 'rivalry_bronze' in result

    def test_counts_losses_too(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(3):
            _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        for _ in range(2):
            m = _make_confirmed_match(p1, p2, [(5, 11), (5, 11), (5, 11)])
        result = check_rivalry(p1, m)
        assert 'rivalry_bronze' in result

    def test_skips_doubles(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        p3 = PlayerFactory()
        p4 = PlayerFactory()
        for _ in range(5):
            m = MatchFactory(
                team1_players=[p1, p2], team2_players=[p3, p4],
                is_double=True, best_of=5,
            )
            GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
            GameFactory(match=m, game_number=2, team1_score=11, team2_score=5)
            GameFactory(match=m, game_number=3, team1_score=11, team2_score=5)
            m.refresh_from_db()
            confirm_match_silent(m)
            m.refresh_from_db()
        result = check_rivalry(p1, m)
        assert result == []


@pytest.mark.django_db
class TestPeakPerformer:
    """A positive change to a new all-time max of `glicko_new_rating` in the
    match's format, never counting a peak below the 1500 start."""

    def test_awarded_on_new_peak(self, achievements):
        # Unrated players start at 1500; the win lifts p1 above it.
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _win(p1, p2)
        assert _all_time_change(m, p1).glicko_new_rating > 1500
        assert check_peak_performer(p1, m) == ['peak_performer']

    def test_not_awarded_on_a_decrease(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(5, 11), (5, 11), (5, 11)])
        assert check_peak_performer(p1, m) == []

    def test_a_rise_that_stays_below_1500_is_not_a_peak(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=1300.0, glicko_new_rating=1499.0)
        assert check_peak_performer(p1, m) == []

    def test_a_rise_below_an_earlier_peak_is_not_a_peak(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        earlier = _win(p1, p2)
        _set_change(earlier, p1, glicko_old_rating=1500.0, glicko_new_rating=1700.0)
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=1600.0, glicko_new_rating=1650.0)

        assert check_peak_performer(p1, m) == []

    def test_a_rise_past_an_earlier_peak_is_a_peak(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        earlier = _win(p1, p2)
        _set_change(earlier, p1, glicko_old_rating=1500.0, glicko_new_rating=1700.0)
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=1650.0, glicko_new_rating=1710.0)

        assert check_peak_performer(p1, m) == ['peak_performer']

    def test_a_zero_change_is_not_a_peak(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=1600.0, glicko_new_rating=1600.0)
        assert check_peak_performer(p1, m) == []

    def test_another_formats_peak_does_not_count(self, achievements):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        doubles = MatchFactory(team1_players=[a, b], team2_players=[c, d], is_double=True, best_of=3)
        GameFactory(match=doubles, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=doubles, game_number=2, team1_score=11, team2_score=5)
        _set_change(doubles, a, glicko_old_rating=1500.0, glicko_new_rating=1900.0)
        assert _all_time_change(doubles, a).rating.format == 'doubles'
        m = _win(a, c)
        _set_change(m, a, glicko_old_rating=1500.0, glicko_new_rating=1520.0)

        assert check_peak_performer(a, m) == ['peak_performer']

    def test_a_skipped_projection_is_not_a_peak(self, achievements):
        p1, p2 = PlayerFactory(), PlayerFactory()
        m = _win(p1, p2)
        _set_change(m, p1, glicko_old_rating=None, glicko_new_rating=None, glicko_change=None)
        assert check_peak_performer(p1, m) == []


# ===========================================================================
# Integration: check_achievements_for_player
# ===========================================================================

@pytest.mark.django_db
class TestCheckAchievementsForPlayer:
    def test_awards_multiple_achievements_at_once(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        # Signal fires during match creation, awarding achievements automatically
        # Iron wall (0+5+5=10 < 20) + first blood + perfect game via 11-0
        _make_confirmed_match(p1, p2, [(11, 0), (11, 5), (11, 5)])
        earned_slugs = set(
            PlayerAchievement.objects.filter(player=p1)
            .values_list('achievement__slug', flat=True)
        )
        assert 'first_blood' in earned_slugs
        assert 'iron_wall' in earned_slugs
        assert 'perfect_game' in earned_slugs

    def test_no_duplicate_awards(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        # first_blood should exist exactly once despite two confirmed matches
        assert PlayerAchievement.objects.filter(
            player=p1, achievement__slug='first_blood'
        ).count() == 1


# ===========================================================================
# Signal integration
# ===========================================================================

@pytest.mark.django_db
class TestAchievementSignalIntegration:
    def test_achievements_awarded_on_match_confirmation(self, achievements):
        """End-to-end: confirm match triggers achievement check."""
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = MatchFactory(player1=p1, player2=p2, best_of=5)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=5)
        m.refresh_from_db()
        # Full confirm with signals
        confirm_match(m)
        # first_blood should have been awarded to p1
        assert PlayerAchievement.objects.filter(
            player=p1, achievement__slug='first_blood'
        ).exists()


# ===========================================================================
# Progress calculation
# ===========================================================================

@pytest.mark.django_db
class TestAchievementProgress:
    def test_progress_with_no_matches(self, achievements):
        p = PlayerFactory()
        progress = get_achievement_progress(p)
        # 6 tiered groups (showing bronze only) + 6 one-offs = 12
        assert len(progress) == 12
        assert all(not item['earned'] for item in progress)

    def test_earned_shows_in_progress(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        m = _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        check_achievements_for_player(p1, m)
        progress = get_achievement_progress(p1)
        first_blood = next(
            item for item in progress
            if item['achievement'].slug == 'first_blood'
        )
        assert first_blood['earned'] is True

    def test_partial_progress_for_tiered(self, achievements):
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        for _ in range(5):
            _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        progress = get_achievement_progress(p1)
        mp_bronze = next(
            item for item in progress
            if item['achievement'].slug == 'matches_played_bronze'
        )
        assert mp_bronze['progress'] == 5
        assert mp_bronze['threshold'] == 10

    def test_giant_slayer_progress_is_the_largest_glicko_gap_won(self, achievements):
        p1, p2, p3 = PlayerFactory(), PlayerFactory(), PlayerFactory()
        small = _win(p1, p2)
        _set_change(small, p1, glicko_old_rating=1400.0)
        _set_change(small, p2, glicko_old_rating=1480.5)  # gap 80.5
        big = _win(p1, p3)
        _set_change(big, p1, glicko_old_rating=1400.0)
        _set_change(big, p3, glicko_old_rating=1650.9)  # gap 250.9
        lost = _make_confirmed_match(p1, p3, [(5, 11), (5, 11), (5, 11)])
        _set_change(lost, p1, glicko_old_rating=1000.0)  # a loss never counts

        progress = get_achievement_progress(p1)

        giant = next(
            item for item in progress
            if item['achievement'].slug.startswith('giant_slayer')
        )
        assert giant['progress'] == 250


# ===========================================================================
# Management command
# ===========================================================================

@pytest.mark.django_db
class TestAwardAchievementsCommand:
    def test_retroactive_awards(self, achievements):
        from django.core.management import call_command
        from io import StringIO

        p1 = PlayerFactory()
        p2 = PlayerFactory()
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])

        out = StringIO()
        call_command('award_achievements', stdout=out)
        assert PlayerAchievement.objects.filter(
            player=p1, achievement__slug='first_blood'
        ).exists()
        assert 'Done' in out.getvalue()

    def test_dry_run_doesnt_save(self, achievements):
        from django.core.management import call_command
        from io import StringIO

        p1 = PlayerFactory()
        p2 = PlayerFactory()
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])
        # Signal already awarded some achievements; clear them to test dry-run
        PlayerAchievement.objects.filter(player=p1).delete()

        out = StringIO()
        call_command('award_achievements', '--dry-run', stdout=out)
        assert not PlayerAchievement.objects.filter(player=p1).exists()
        assert 'DRY RUN' in out.getvalue()

    def test_idempotent(self, achievements):
        from django.core.management import call_command
        from io import StringIO

        p1 = PlayerFactory()
        p2 = PlayerFactory()
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])

        call_command('award_achievements', stdout=StringIO())
        count1 = PlayerAchievement.objects.filter(player=p1).count()
        call_command('award_achievements', stdout=StringIO())
        count2 = PlayerAchievement.objects.filter(player=p1).count()
        assert count1 == count2

    def test_single_player_flag(self, achievements):
        from django.core.management import call_command
        from io import StringIO

        p1 = PlayerFactory()
        p2 = PlayerFactory()
        _make_confirmed_match(p1, p2, [(11, 5), (11, 5), (11, 5)])

        call_command('award_achievements', '--player', str(p1.pk), stdout=StringIO())
        assert PlayerAchievement.objects.filter(player=p1).exists()
        # p2 should NOT have been processed (they lost, so no first_blood anyway,
        # but the command should have only iterated over p1)


@pytest.mark.django_db
class TestPlayerSideHelpers:
    """The Team-based _player_team/_opponent_team helpers became side lookups."""

    def _doubles(self):
        players = [PlayerFactory(with_user=True) for _ in range(4)]
        m = MatchFactory(
            team1_players=players[:2], team2_players=players[2:], is_double=True
        )
        return players, m

    def test_side_lookup_for_a_side_two_player(self):
        from pingpong.achievements import _opponent_side, _player_side
        from pingpong.models import Side

        players, m = self._doubles()

        assert _player_side(players[2], m) == Side.TWO
        assert _opponent_side(players[2], m) == Side.ONE
        assert _player_side(players[0], m) == Side.ONE
        assert _opponent_side(players[0], m) == Side.TWO

    def test_side_lookup_for_a_non_participant_is_none(self):
        from pingpong.achievements import _opponent_side, _player_side

        _, m = self._doubles()
        outsider = PlayerFactory(with_user=True)

        assert _player_side(outsider, m) is None
        assert _opponent_side(outsider, m) is None

    def test_doubles_partner_on_the_winning_side_counts_as_a_win(self):
        from pingpong.achievements import _player_won_count, _player_won_match

        players, m = self._doubles()
        for n in (1, 2, 3):
            GameFactory(match=m, game_number=n, team1_score=11, team2_score=4)
        m.refresh_from_db()
        confirm_match_silent(m)
        m.refresh_from_db()

        assert _player_won_match(players[0], m) is True
        assert _player_won_match(players[1], m) is True
        assert _player_won_match(players[2], m) is False
        assert _player_won_count(players[1]) == 1
        assert _player_won_count(players[2]) == 0

    def test_confirmed_matches_are_not_duplicated_for_doubles(self):
        """The old Q(team1__players)|Q(team2__players) OR needed .distinct();
        one participant row per player per match means it no longer does.
        """
        from pingpong.achievements import _player_confirmed_matches

        players, m = self._doubles()
        for n in (1, 2, 3):
            GameFactory(match=m, game_number=n, team1_score=11, team2_score=4)
        m.refresh_from_db()
        confirm_match_silent(m)

        pks = list(_player_confirmed_matches(players[0]).values_list("pk", flat=True))
        assert pks == [m.pk]

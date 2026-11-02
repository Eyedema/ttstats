"""S2.3 -- the pure point-statistics module (KAN-29).

**No `@pytest.mark.django_db` anywhere in this file, and that is the point.**
The marker's absence is what proves `point_stats.py` stayed pure: it takes
plain tuples, touches no database and needs no settings, exactly like
`live_scoring.py`, `match_state.py` and `player_hues.py` before it.

Expected values are worked out by hand in the test rather than derived from
the code under test.
"""
import os
import subprocess
import sys
import textwrap

import pytest

from pingpong import point_stats as ps
from pingpong.point_stats import SIDE_ONE, SIDE_TWO, Point


def points_of(game_number, sides, servers=None):
    """Build a game's Point sequence from the order sides scored.

    ``servers`` is an optional parallel list; where omitted the server is left
    unknown, which is itself a case worth exercising.
    """
    out = []
    t1 = t2 = 0
    for index, side in enumerate(sides):
        if side == SIDE_ONE:
            t1 += 1
        else:
            t2 += 1
        server = servers[index] if servers else None
        out.append(Point(game_number, side, server, t1, t2))
    return out


def alternating_servers(count, first=SIDE_ONE):
    """Two serves each, which is the rule below 20 points combined."""
    servers = []
    for i in range(count):
        servers.append(first if (i // 2) % 2 == 0 else ps.other_side(first))
    return servers


ONE, TWO = SIDE_ONE, SIDE_TWO


class TestTheZeroState:
    def test_empty_sequence_returns_a_defined_zero_state(self):
        stats = ps.match_stats([])

        assert stats.has_data is False
        assert stats.total_points == 0
        assert stats.games == []
        for side in (ONE, TWO):
            s = stats.side(side)
            assert s.serve.rate == 0.0
            assert s.receive.rate == 0.0
            assert s.points.rate == 0.0
            assert s.deuce.rate == 0.0
            assert s.comebacks == 0
            assert s.largest_deficit_overcome == 0
            assert s.longest_run == 0

    def test_a_rate_with_no_sample_is_zero_not_an_error(self):
        rate = ps.Rate()
        assert rate.rate == 0.0
        assert rate.percent == 0.0
        assert rate.has_sample is False

    def test_game_stats_of_nothing_is_none(self):
        assert ps.game_stats([]) is None


class TestServeAndReceive:
    def test_shutout_gives_one_side_every_point_and_the_other_no_denominator(self):
        # 11-0. Side one serves points 1-2 and 5-6 and 9-10; side two the rest.
        sides = [ONE] * 11
        servers = alternating_servers(11, first=ONE)
        stats = ps.match_stats(points_of(1, sides, servers))

        one = stats.side(ONE)
        two = stats.side(TWO)

        # Independently: servers are 1,1,2,2,1,1,2,2,1,1,2 -> six by side one.
        assert servers.count(ONE) == 6
        assert one.serve == ps.Rate(6, 6)
        assert one.serve.rate == 1.0
        assert one.receive == ps.Rate(5, 5)
        assert one.points == ps.Rate(11, 11)

        assert two.serve == ps.Rate(0, 5)
        assert two.serve.rate == 0.0
        assert two.receive == ps.Rate(0, 6)
        assert two.points == ps.Rate(0, 11)

    def test_served_points_of_both_sides_sum_to_the_points_played(self):
        sides = [ONE, TWO, ONE, ONE, TWO, TWO, ONE]
        servers = alternating_servers(len(sides))
        stats = ps.match_stats(points_of(1, sides, servers))

        total = stats.side(ONE).serve.total + stats.side(TWO).serve.total
        assert total == len(sides)

    def test_a_side_serve_plus_receive_covers_every_point_it_played(self):
        sides = [ONE, TWO, ONE, ONE, TWO]
        servers = alternating_servers(len(sides))
        stats = ps.match_stats(points_of(1, sides, servers))

        one = stats.side(ONE)
        assert one.serve.total + one.receive.total == 5
        assert one.serve.won + one.receive.won == one.points.won

    def test_a_point_with_no_recorded_server_counts_toward_neither(self):
        sides = [ONE, TWO, ONE]
        servers = [ONE, None, TWO]
        stats = ps.match_stats(points_of(1, sides, servers))

        one = stats.side(ONE)
        assert one.serve == ps.Rate(1, 1)
        assert one.receive == ps.Rate(1, 1)
        assert one.points == ps.Rate(2, 3)  # the point still counts


class TestDeuce:
    def test_a_game_reaching_ten_all_is_a_deuce_game(self):
        sides = ([ONE, TWO] * 10) + [ONE, ONE]  # 12-10
        game = ps.game_stats(points_of(1, sides))

        assert (game.team1_score, game.team2_score) == (12, 10)
        assert game.is_deuce is True
        assert game.complete is True
        assert game.winner_side == ONE

    def test_a_long_deuce_game_is_still_one_deuce_game(self):
        # 20-18: level to 18-18, then two for side one.
        sides = ([ONE, TWO] * 18) + [ONE, ONE]
        stats = ps.match_stats(points_of(1, sides))

        assert (stats.games[0].team1_score, stats.games[0].team2_score) == (20, 18)
        assert stats.games[0].is_deuce is True
        assert stats.side(ONE).deuce == ps.Rate(1, 1)
        assert stats.side(TWO).deuce == ps.Rate(0, 1)

    def test_an_ordinary_game_is_not_a_deuce_game(self):
        sides = ([ONE, TWO] * 9) + [ONE, ONE]  # 11-9
        game = ps.game_stats(points_of(1, sides))
        assert game.is_deuce is False


class TestComebacks:
    def test_a_game_won_from_five_nine_down_overcame_a_deficit_of_four(self):
        sides = ([ONE, TWO] * 5) + [TWO] * 4 + [ONE] * 6  # 5-5, 5-9, 11-9
        game = ps.game_stats(points_of(1, sides))

        assert (game.team1_score, game.team2_score) == (11, 9)
        assert game.winner_side == ONE
        assert game.largest_deficit[ONE] == 4
        assert game.largest_deficit[TWO] == 2
        assert game.deficit_overcome == 4
        assert game.is_comeback is True

    def test_a_two_point_swing_is_not_a_comeback(self):
        # 0-2 down, won 11-2. Two points behind is an ordinary swing.
        sides = [TWO, TWO] + [ONE] * 11
        game = ps.game_stats(points_of(1, sides))

        assert game.largest_deficit[ONE] == 2
        assert game.deficit_overcome == 2
        assert game.is_comeback is False
        assert ps.COMEBACK_DEFICIT == 3

    def test_the_loser_of_a_game_is_credited_with_no_comeback(self):
        sides = ([ONE, TWO] * 5) + [TWO] * 4 + [ONE] * 6
        stats = ps.match_stats(points_of(1, sides))

        assert stats.side(ONE).comebacks == 1
        assert stats.side(ONE).largest_deficit_overcome == 4
        assert stats.side(TWO).comebacks == 0
        assert stats.side(TWO).largest_deficit_overcome == 0


class TestLeadChangesAndRuns:
    def test_returning_to_level_is_not_a_lead_change(self):
        # 1-0, 1-1, 2-1: the leader never actually swapped.
        game = ps.game_stats(points_of(1, [ONE, TWO, ONE]))
        assert game.lead_changes == 0

    def test_the_leader_swapping_is_a_lead_change(self):
        # 1-0 then two for side two -> 1-2.
        game = ps.game_stats(points_of(1, [ONE, TWO, TWO]))
        assert game.lead_changes == 1

    def test_the_five_nine_comeback_has_two_lead_changes(self):
        sides = ([ONE, TWO] * 5) + [TWO] * 4 + [ONE] * 6
        game = ps.game_stats(points_of(1, sides))
        # Side one leads early, side two takes it at 5-6, side one at 10-9.
        assert game.lead_changes == 2

    def test_longest_run_is_the_longest_streak_of_consecutive_points(self):
        sides = ([ONE, TWO] * 5) + [TWO] * 4 + [ONE] * 6
        stats = ps.match_stats(points_of(1, sides))

        assert stats.side(ONE).longest_run == 6
        assert stats.side(TWO).longest_run == 5  # the 5-5 point plus four more

    def test_progression_starts_at_the_origin(self):
        game = ps.game_stats(points_of(1, [ONE, TWO]))
        assert game.progression == [(0, 0), (1, 0), (1, 1)]


class TestPartialMatches:
    def test_an_abandoned_game_is_excluded_from_game_level_stats_only(self):
        points = points_of(1, [ONE] * 11) + points_of(2, [TWO] * 3)

        stats = ps.match_stats(points)

        assert len(stats.games) == 2
        assert stats.games[0].complete is True
        assert stats.games[1].complete is False
        assert stats.games[1].winner_side is None

        one = stats.side(ONE)
        two = stats.side(TWO)
        # Game-level: only the finished game counts.
        assert one.games == ps.Rate(1, 1)
        assert two.games == ps.Rate(0, 1)
        # Point-level: every point counts, the unfinished game included.
        assert one.points == ps.Rate(11, 14)
        assert two.points == ps.Rate(3, 14)
        assert two.longest_run == 3

    def test_a_match_with_a_single_point_does_not_blow_up(self):
        stats = ps.match_stats(points_of(1, [ONE]))
        assert stats.total_points == 1
        assert stats.games[0].complete is False
        assert stats.side(ONE).games == ps.Rate(0, 0)
        assert stats.side(ONE).games.rate == 0.0


class TestSplittingGames:
    def test_games_are_split_on_consecutive_runs_of_game_number(self):
        points = (
            points_of(1, [ONE] * 11)
            + points_of(2, [TWO] * 11)
            + points_of(3, [ONE] * 11)
        )
        split = ps.split_games(points)

        assert [len(g) for g in split] == [11, 11, 11]
        assert [g[0].game_number for g in split] == [1, 2, 3]

    def test_a_full_match_credits_the_right_number_of_games(self):
        points = (
            points_of(1, [ONE] * 11)
            + points_of(2, [TWO] * 11)
            + points_of(3, [ONE] * 11)
        )
        stats = ps.match_stats(points)

        assert stats.side(ONE).games == ps.Rate(2, 3)
        assert stats.side(TWO).games == ps.Rate(1, 3)


class TestCombiningAcrossMatches:
    def test_rates_add_by_numerator_and_denominator_not_by_averaging(self):
        a = ps.SideStats(side=ONE, serve=ps.Rate(1, 2), points=ps.Rate(1, 2))
        b = ps.SideStats(side=ONE, serve=ps.Rate(9, 10), points=ps.Rate(9, 10))

        combined = ps.combine_side_stats([a, b], side=ONE)

        assert combined.serve == ps.Rate(10, 12)
        # Averaging the two rates would give 0.70; the honest figure is 0.833.
        assert combined.serve.rate == pytest.approx(10 / 12)

    def test_records_of_a_single_best_take_the_maximum_never_the_sum(self):
        a = ps.SideStats(side=ONE, longest_run=6, largest_deficit_overcome=4)
        b = ps.SideStats(side=ONE, longest_run=3, largest_deficit_overcome=7)

        combined = ps.combine_side_stats([a, b], side=ONE)

        assert combined.longest_run == 6
        assert combined.largest_deficit_overcome == 7

    def test_counts_add(self):
        a = ps.SideStats(side=ONE, comebacks=1, deuce=ps.Rate(1, 2))
        b = ps.SideStats(side=ONE, comebacks=2, deuce=ps.Rate(0, 1))

        combined = ps.combine_side_stats([a, b], side=ONE)

        assert combined.comebacks == 3
        assert combined.deuce == ps.Rate(1, 3)

    def test_combining_nothing_is_the_zero_state(self):
        combined = ps.combine_side_stats([], side=TWO)
        assert combined == ps.SideStats(side=TWO)

    def test_a_career_fold_equals_recomputing_from_every_point(self):
        """What makes S2.4's rollups sufficient rather than merely faster."""
        match_a = points_of(1, ([ONE, TWO] * 5) + [ONE] * 6, alternating_servers(16))
        match_b = points_of(1, [TWO] * 11, alternating_servers(11))

        folded = ps.combine_side_stats(
            [ps.match_stats(match_a).side(ONE), ps.match_stats(match_b).side(ONE)],
            side=ONE,
        )

        # Recomputed from the raw points of both matches at once. Game-level
        # figures need the games kept apart, so compare the point-level ones,
        # which are what a career rate actually is.
        every_point = ps.match_stats(match_a).side(ONE)
        other = ps.match_stats(match_b).side(ONE)
        assert folded.serve == every_point.serve + other.serve
        assert folded.receive == every_point.receive + other.receive
        # Side one won 5 of the alternating points plus the closing 6, and
        # none of match_b's eleven: 11 of 27.
        assert folded.points == ps.Rate(11, 27)


class TestPropertiesOverGeneratedSequences:
    """Invariants that must hold for any sequence, not just the worked cases."""

    @pytest.mark.parametrize("seed", range(30))
    def test_serve_won_plus_receive_won_equals_points_won(self, seed):
        import random

        rng = random.Random(seed)
        length = rng.randint(1, 60)
        sides = [rng.choice([ONE, TWO]) for _ in range(length)]
        servers = [rng.choice([ONE, TWO, None]) for _ in range(length)]
        stats = ps.match_stats(points_of(1, sides, servers))

        known = sum(1 for s in servers if s is not None)
        for side in (ONE, TWO):
            s = stats.side(side)
            assert s.serve.total + s.receive.total == known
            assert s.serve.won + s.receive.won <= s.points.won

        assert (
            stats.side(ONE).points.won + stats.side(TWO).points.won == length
        )
        assert stats.side(ONE).serve.total + stats.side(TWO).serve.total == known

    @pytest.mark.parametrize("seed", range(30))
    def test_no_rate_ever_leaves_the_unit_interval(self, seed):
        import random

        rng = random.Random(seed)
        length = rng.randint(0, 40)
        sides = [rng.choice([ONE, TWO]) for _ in range(length)]
        stats = ps.match_stats(points_of(1, sides))

        for side in (ONE, TWO):
            s = stats.side(side)
            for rate in (s.serve, s.receive, s.points, s.games, s.deuce):
                assert 0.0 <= rate.rate <= 1.0
                assert rate.won <= rate.total


class TestPurity:
    def test_the_module_imports_with_no_django_settings_configured(self):
        """The marker that this module stayed a pure decision module.

        Run in a subprocess with DJANGO_SETTINGS_MODULE stripped: importing
        anything Django-touching would raise ImproperlyConfigured.
        """
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("DJANGO_SETTINGS_MODULE",)
        }
        source = textwrap.dedent(
            """
            import django.conf
            assert not django.conf.settings.configured, "settings already configured"
            from pingpong import point_stats
            stats = point_stats.match_stats([])
            assert stats.total_points == 0
            print("pure")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "pure" in result.stdout

    def test_the_source_file_names_no_django_import(self):
        import pathlib

        source = pathlib.Path(ps.__file__).read_text()
        assert "import django" not in source
        assert "from django" not in source

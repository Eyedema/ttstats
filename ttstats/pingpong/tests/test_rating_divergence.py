"""The Elo vs Glicko-2 divergence harness (KAN-66 / Q6.3).

The harness walks history through the shared replay and must be deterministic,
sensitive to the Glicko parameters, correct on a hand-checked fixture, and
strictly read-only.
"""

import json
import math
from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from ..glicko_adapter import DEFAULT_PARAMS
from ..models import EloHistory, Game, Match, Player, PlayerRating, RatingChange, Side
from ..rating_divergence import (
    PredictionStats,
    kendall_tau_b,
    ordinal_ranks,
    rd_histogram,
    run_divergence,
)
from .conftest import GameFactory, MatchFactory, PlayerFactory

BASE = timezone.make_aware(datetime(2025, 1, 1, 12, 0, 0))


def _play(side1, side2, *, day=0, winner=Side.ONE, best_of=5, match_type="casual"):
    match = MatchFactory(
        team1_players=list(side1),
        team2_players=list(side2),
        is_double=len(side1) > 1,
        best_of=best_of,
        match_type=match_type,
        date_played=BASE + timedelta(days=day),
    )
    for n in range(1, best_of // 2 + 2):
        score = (11, 6) if winner == Side.ONE else (6, 11)
        GameFactory(
            match=match, game_number=n, team1_score=score[0], team2_score=score[1]
        )
    return match


def _history():
    """Upsets, gaps, a one-match player, and a doubles match."""
    ada, bob, cy, di, eve = (
        PlayerFactory(name=n) for n in ("Ada", "Bob", "Cy", "Di", "Eve")
    )
    day = 0
    for i in range(12):
        _play([ada], [bob], day=day, winner=Side.ONE if i % 3 else Side.TWO)
        _play([bob], [cy], day=day + 1, winner=Side.TWO if i % 4 else Side.ONE)
        _play([ada], [cy], day=day + 2, winner=Side.ONE if i % 5 else Side.TWO)
        day += 3 + (i % 4) * 20
    _play([di], [eve], day=1)  # tiny sample, long ago
    _play([ada, bob], [cy, di], day=day, winner=Side.TWO)
    return ada, bob, cy, di, eve


def _singles(report):
    return next(f for f in report.formats if f.format == "singles")


def _doubles(report):
    return next(f for f in report.formats if f.format == "doubles")


@pytest.mark.django_db
class TestDeterminism:
    def test_running_twice_gives_an_identical_report(self):
        _history()
        a, b = run_divergence(), run_divergence()
        assert a.render() == b.render()
        assert a.as_dict() == b.as_dict()
        json.dumps(a.as_dict())  # serialisable

    def test_small_chunk_size_gives_the_same_result(self):
        _history()
        assert run_divergence(chunk_size=3).as_dict() == run_divergence().as_dict()


@pytest.mark.django_db
class TestSensitivity:
    def _finals(self, report):
        return {
            p.name: (p.glicko, p.rd)
            for p in _singles(report).players + _singles(report).not_comparable
        }

    def test_tau_changes_the_glicko_finals_and_the_report(self):
        _history()
        base = run_divergence(tiny_rd=1000)
        other = run_divergence(params=DEFAULT_PARAMS._replace(tau=1.2), tiny_rd=1000)
        assert self._finals(base) != self._finals(other)
        assert base.render() != other.render()

    def test_period_days_changes_the_glicko_finals(self):
        _history()
        base = run_divergence(tiny_rd=1000)
        other = run_divergence(
            params=DEFAULT_PARAMS._replace(period_days=60), tiny_rd=1000
        )
        assert self._finals(base) != self._finals(other)

    def test_elo_is_untouched_by_glicko_params(self):
        _history()
        base = run_divergence(tiny_rd=1000)
        other = run_divergence(params=DEFAULT_PARAMS._replace(tau=1.2), tiny_rd=1000)
        elo = lambda r: {p.name: p.elo for p in _singles(r).players}
        assert elo(base) == elo(other)
        assert (
            base.formats[0].prediction["elo"]["all"].as_dict()
            == other.formats[0].prediction["elo"]["all"].as_dict()
        )


@pytest.mark.django_db
class TestPredictionOnAHandCheckedFixture:
    def test_brier_log_loss_and_favourite_rate(self):
        ada, bob = PlayerFactory(name="Ada"), PlayerFactory(name="Bob")
        _play([ada], [bob], day=0, winner=Side.ONE)  # 1500 v 1500: exact tie
        _play([ada], [bob], day=1, winner=Side.ONE)
        _play([ada], [bob], day=2, winner=Side.TWO)

        # Best of 5 casual, both provisional: K = 32 * 1.5 = 48.
        p1 = 0.5
        r_ada = 1500 + round(48 * (1 - p1))  # 1524
        r_bob = 1500 - round(48 * (1 - p1))
        p2 = 1 / (1 + 10 ** (-(r_ada - r_bob) / 400))
        r_ada += round(48 * (1 - p2))
        r_bob -= round(48 * (1 - p2))
        p3 = 1 / (1 + 10 ** (-(r_ada - r_bob) / 400))

        briers = [(p1 - 1) ** 2, (p2 - 1) ** 2, (p3 - 0) ** 2]
        losses = [-math.log(p1), -math.log(p2), -math.log(1 - p3)]

        stats = _singles(run_divergence()).prediction["elo"]["all"]
        assert stats.n == 3
        assert stats.non_tie == 2  # match 1 is an exact 0.5: no favourite
        assert stats.hits == 1  # the favourite won match 2, lost match 3
        assert stats.favourite_rate == pytest.approx(0.5)
        assert stats.brier == pytest.approx(sum(briers) / 3)
        assert stats.log_loss == pytest.approx(sum(losses) / 3)

    def test_glicko_is_also_scored_and_nobody_is_established(self):
        ada, bob = PlayerFactory(name="Ada"), PlayerFactory(name="Bob")
        _play([ada], [bob], day=0)
        _play([ada], [bob], day=1)
        report = _singles(run_divergence())
        assert report.prediction["glicko"]["all"].n == 2
        assert report.prediction["glicko"]["all"].non_tie == 1  # first is 0.5 exactly
        assert report.prediction["elo"]["established"].n == 0
        assert report.prediction["elo"]["established"].brier is None

    def test_established_subset_needs_both_sides_established(self):
        ada, bob, cy = (PlayerFactory(name=n) for n in ("Ada", "Bob", "Cy"))
        for i in range(21):
            _play([ada], [bob], day=i)
        _play([ada], [cy], day=40)  # Cy is new
        _play([ada], [bob], day=41)  # both have > 20
        report = _singles(run_divergence())
        assert report.prediction["elo"]["all"].n == 23
        assert (
            report.prediction["elo"]["established"].n == 2
        )  # match 21 (20 prior each) and match 23; match 22 has new Cy

    def test_unit_stats_exact_tie_is_not_a_favourite(self):
        s = PredictionStats()
        s.add(0.5, True)
        s.add(0.8, True)
        s.add(0.8, False)
        assert (s.n, s.non_tie, s.hits) == (3, 2, 1)


class TestKendall:
    def test_identical_ordering_is_one(self):
        assert kendall_tau_b([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_reversed_ordering_is_minus_one(self):
        assert kendall_tau_b([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_hand_computed_mixed_case(self):
        # pairs: (1,2)C (1,3)C (1,4)C (2,3)D (2,4)C (3,4)C -> 5 C, 1 D, tau = 4/6
        assert kendall_tau_b([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(4 / 6)

    def test_ties_use_tau_b(self):
        # x ties on (1,2); C: (1,3) (2,3); n0=3, n1=1 -> 2 / sqrt(3-1 ... )
        xs, ys = [1, 1, 2], [1, 2, 3]
        # C=2, D=0, ties_x=1, ties_y=0 -> 2 / sqrt(3 * 2)
        assert kendall_tau_b(xs, ys) == pytest.approx(2 / math.sqrt(6))

    def test_undefined_cases_are_none(self):
        assert kendall_tau_b([], []) is None
        assert kendall_tau_b([1], [1]) is None
        assert kendall_tau_b([5, 5], [1, 2]) is None

    def test_ranks_break_ties_by_id(self):
        assert ordinal_ranks([1500, 1500, 1600], [7, 3, 9]) == [3, 2, 1]

    def test_rd_histogram_counts_every_value(self):
        bins = rd_histogram([10, 50, 349, 350, 200])
        assert sum(b["count"] for b in bins) == 5
        assert bins[-1]["count"] == 2


@pytest.mark.django_db
class TestReportContent:
    def test_tiny_sample_player_is_listed_as_not_comparable(self):
        _history()
        report = _singles(run_divergence(tiny_rd=250))
        not_comparable = {p.name for p in report.not_comparable}
        assert {"Di", "Eve"} <= not_comparable
        assert "Ada" in {p.name for p in report.players}
        assert not not_comparable & {p.name for p in report.players}
        text = run_divergence(tiny_rd=250).render()
        assert "Not comparable" in text

    def test_ranks_and_movers_are_consistent(self):
        _history()
        report = _singles(run_divergence(tiny_rd=250))
        assert sorted(p.elo_rank for p in report.players) == list(
            range(1, len(report.players) + 1)
        )
        for p in report.players:
            assert p.rank_change == p.elo_rank - p.glicko_rank
        changes = [abs(p.rank_change) for p in report.movers]
        assert changes == sorted(changes, reverse=True)
        assert report.tau is not None
        assert -1 <= report.tau <= 1

    def test_end_rd_is_inflated_to_the_last_match_not_now(self):
        _history()
        report = run_divergence()
        assert str(report.history_end) in report.render()
        assert (
            report.history_end
            == max(Match.objects.values_list("date_played", flat=True)).date()
        )

    def test_doubles_have_their_own_section_and_do_not_crash(self):
        _history()
        report = run_divergence()
        doubles = _doubles(report)
        assert doubles.matches == 1
        assert doubles.glicko_rated is False
        assert doubles.prediction["elo"]["all"].n == 1
        text = report.render()
        assert "DOUBLES" in text
        assert "not rated under Glicko-2 yet" in text
        assert doubles.as_dict()["kendall_tau_b"] is None

    def test_rateable_set_is_the_confirmed_one(self):
        ada, bob = PlayerFactory(name="Ada"), PlayerFactory(name="Bob")
        _play([ada], [bob])
        MatchFactory(player1=ada, player2=bob)  # no games: no winner
        report = run_divergence()
        assert report.rated_matches == 1
        assert "confirmed matches" in report.render()

    def test_empty_history_renders_a_sensible_empty_report(self):
        report = run_divergence()
        text = report.render()
        assert report.rated_matches == 0
        assert "Rated matches: 0" in text
        assert "SINGLES: 0 matches" in text
        assert _singles(report).players == []
        assert _singles(report).tau is None
        json.dumps(report.as_dict())


@pytest.mark.django_db
class TestCommand:
    def test_smoke(self):
        _history()
        out = StringIO()
        call_command("rating_divergence", stdout=out)
        assert "Kendall tau-b" in out.getvalue()
        assert "DOUBLES" in out.getvalue()

    def test_options_are_applied(self):
        _history()
        out = StringIO()
        call_command(
            "rating_divergence",
            "--tau",
            "0.3",
            "--period-days",
            "14",
            "--season-rd-floor",
            "120",
            "--top",
            "2",
            "--chunk-size",
            "4",
            stdout=out,
        )
        assert "tau=0.3 period_days=14.0 season_rd_floor=120.0" in out.getvalue()

    def test_json(self):
        _history()
        out = StringIO()
        call_command("rating_divergence", "--json", stdout=out)
        data = json.loads(out.getvalue())
        assert data["params"]["tau"] == DEFAULT_PARAMS.tau
        assert {f["format"] for f in data["formats"]} == {"singles", "doubles"}

    def test_it_writes_nothing(self):
        _history()

        def snapshot():
            counts = [
                m.objects.count() if hasattr(m.objects, "count") else 0
                for m in (Match, Game, Player)
            ]
            counts += [
                m._base_manager.count()
                for m in (PlayerRating, RatingChange, EloHistory)
            ]
            ratings = sorted(
                PlayerRating._base_manager.values_list(
                    "pk",
                    "rating",
                    "peak",
                    "matches",
                    "glicko_rating",
                    "rd",
                    "volatility",
                    "last_rated_on",
                ),
                key=lambda r: r[0],
            )
            players = sorted(
                Player.objects.values_list("pk", "elo_rating", "matches_for_elo")
            )
            return counts, ratings, players

        before = snapshot()
        call_command("rating_divergence", stdout=StringIO())
        call_command("rating_divergence", "--json", stdout=StringIO())
        assert snapshot() == before

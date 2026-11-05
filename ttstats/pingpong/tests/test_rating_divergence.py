"""The Elo vs Glicko-2 divergence harness (KAN-66 / Q6.3).

The harness walks history through the shared replay and must be deterministic,
sensitive to the Glicko parameters, correct on a hand-checked fixture, and
strictly read-only.
"""

import json
import math
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from .. import glicko2, rating_divergence
from ..elo_replay import iter_replay_matches
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

    def test_doubles_have_their_own_glicko_section_and_do_not_crash(self):
        # Changed deliberately by KAN-70: doubles used to be "not rated yet".
        _history()
        report = run_divergence(tiny_rd=1000)
        singles, doubles = _singles(report), _doubles(report)
        assert doubles.matches == 1
        assert doubles.glicko_rated is True
        assert doubles.prediction["elo"]["all"].n == 1
        assert doubles.prediction["glicko"]["all"].n == 1
        assert doubles.skipped_shadow == 0 and singles.skipped_shadow == 0
        assert len(doubles.players) == 4  # Ada, Bob, Cy, Di
        assert all(p.glicko is not None and p.rd is not None for p in doubles.players)
        text = report.render()
        assert "DOUBLES" in text
        assert "not rated under Glicko-2" not in text
        doubles_text = text[text.index("DOUBLES") :]
        assert "Kendall tau-b" in doubles_text
        assert "Glicko projections skipped in doubles: 0" in text
        assert doubles.as_dict()["glicko_rated"] is True
        assert doubles.as_dict()["kendall_tau_b"] is not None
        # Singles ratings are not touched by the doubles match.
        assert {p.name for p in singles.players} == {"Ada", "Bob", "Cy", "Di", "Eve"}

    def test_a_doubles_prediction_is_the_composite_win_probability(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        _play([a, b], [c, d], day=0, winner=Side.TWO)
        report = run_divergence(tiny_rd=1000)
        # Everyone unrated: 1500 / RD 350 on both sides, so exactly 0.5.
        stats = _doubles(report).prediction["glicko"]["all"]
        assert stats.n == 1
        assert stats.brier == pytest.approx((0.5 - 0.0) ** 2)

    def test_skipped_doubles_projections_are_reported_apart_from_singles(self):
        _history()
        real = rating_divergence.glicko_adapter.project

        def doubles_fail(match, scope, book, params):
            if match.is_double:
                raise glicko2.Glicko2Error("doubles only")
            return real(match, scope, book, params)

        with mock.patch.object(
            rating_divergence.glicko_adapter, "project", doubles_fail
        ):
            report = run_divergence(tiny_rd=1000)
        assert _doubles(report).skipped_shadow == 1
        assert _singles(report).skipped_shadow == 0
        assert report.skipped_shadow == 1
        assert "Glicko projections skipped in doubles: 1" in report.render()

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
        real = rating_divergence.iter_replay_matches
        with mock.patch.object(
            rating_divergence, "iter_replay_matches", wraps=real
        ) as walk:
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
                "--tiny-rd",
                "1000",
                stdout=out,
            )
        assert "tau=0.3 period_days=14.0 season_rd_floor=120.0" in out.getvalue()
        assert walk.call_args.kwargs["chunk_size"] == 4
        out = StringIO()
        call_command(
            "rating_divergence", "--top", "2", "--tiny-rd", "1000", "--json", stdout=out
        )
        singles = next(
            f for f in json.loads(out.getvalue())["formats"] if f["format"] == "singles"
        )
        assert len(singles["players"]) > 2
        assert len(singles["movers"]) == 2

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


def _reference(records, params=DEFAULT_PARAMS):
    """Hand-rolled singles Glicko-2 on plain dicts: the independent oracle.

    Returns (final state by player, last day by player, per-match predictions).
    """
    state, last, predictions = {}, {}, []
    for m in records:
        day = m.date_played.date()
        a, b = m.side1_ids[0], m.side2_ids[0]
        pre = {}
        for pid in (a, b):
            if pid not in state:
                pre[pid] = glicko2.Rating(
                    1500, params.rd_max, params.initial_volatility
                )
            else:
                periods = max(0, (day - last[pid]).days) / params.period_days
                pre[pid] = glicko2.inflate(state[pid], periods, rd_max=params.rd_max)
        s = 1.0 if m.winner_side == 1 else 0.0
        predictions.append(
            (
                glicko2.win_probability(
                    pre[a].rating, pre[a].rd, pre[b].rating, pre[b].rd
                ),
                s,
            )
        )
        new_a = glicko2.rate(
            pre[a],
            [glicko2.Game(pre[b].rating, pre[b].rd, s)],
            params.tau,
            rd_max=params.rd_max,
        )
        new_b = glicko2.rate(
            pre[b],
            [glicko2.Game(pre[a].rating, pre[a].rd, 1 - s)],
            params.tau,
            rd_max=params.rd_max,
        )
        state[a], state[b] = new_a, new_b
        last[a] = max(day, last.get(a, day))
        last[b] = max(day, last.get(b, day))
    return state, last, predictions


def _gapped_history():
    a, b, c = (PlayerFactory(name=n) for n in ("Ada", "Bob", "Cy"))
    pattern = [
        (a, b, 1),
        (a, b, 2),
        (b, c, 1),
        (a, c, 1),
        (a, b, 1),
        (c, a, 2),
        (b, c, 2),
        (a, b, 1),
        (a, c, 2),
        (c, b, 1),
        (a, b, 2),
        (b, c, 1),
    ]
    gaps = [0, 1, 7, 30, 265, 0, 3, 14, 60, 0, 9, 120]
    day = 0
    for (x, y, w), gap in zip(pattern, gaps):
        day += gap
        _play([x], [y], day=day, winner=Side.ONE if w == 1 else Side.TWO)
    return a, b, c


@pytest.mark.django_db
class TestGlickoArithmeticPinned:
    def test_final_state_and_predictions_equal_the_hand_reference_exactly(self):
        _gapped_history()
        records = list(iter_replay_matches())
        state, last, predictions = _reference(records)
        report = run_divergence(tiny_rd=1000)
        fr = _singles(report)

        end = records[-1].date_played.date()
        by_id = {p.player_id: p for p in fr.players}
        for pid, rating in state.items():
            periods = max(0, (end - last[pid]).days) / DEFAULT_PARAMS.period_days
            expected = glicko2.inflate(rating, periods, rd_max=DEFAULT_PARAMS.rd_max)
            assert by_id[pid].glicko == expected.rating
            assert by_id[pid].rd == expected.rd

        # Match k is predicted from the state after k-1 matches only (no leakage).
        # Plain sequential accumulation (builtin sum() is compensated on 3.12).
        brier_sum = loss_sum = 0.0
        for p, s in predictions:
            brier_sum += (p - s) ** 2
            loss_sum += -math.log(min(max(p if s else 1 - p, 1e-15), 1 - 1e-15))
        brier, loss = brier_sum / len(predictions), loss_sum / len(predictions)
        stats = fr.prediction["glicko"]["all"]
        assert stats.n == len(records)
        assert stats.brier == brier
        assert stats.log_loss == loss

    def test_first_prediction_uses_only_the_unrated_state(self):
        _gapped_history()
        records = list(iter_replay_matches())
        _, _, predictions = _reference(records)
        assert predictions[0][0] == 0.5

    def test_glicko_is_not_zero_sum(self):
        _gapped_history()
        fr = _singles(run_divergence(tiny_rd=1000))
        state, _, _ = _reference(list(iter_replay_matches()))
        assert fr.glicko_sum == sum(r.rating for r in state.values()) or math.isclose(
            fr.glicko_sum, sum(r.rating for r in state.values()), rel_tol=1e-9
        )
        assert abs(fr.glicko_sum - 1500 * len(state)) > 1  # the level shifts

    def test_rating_sums_are_reported(self):
        _gapped_history()
        report = run_divergence(tiny_rd=1000)
        fr = _singles(report)
        assert fr.elo_sum == sum(p.elo for p in fr.players)
        assert "Rating sum" in report.render()
        assert report.as_dict()["formats"][0]["rating_sum"]["elo"] == fr.elo_sum


@pytest.mark.django_db
class TestAccountingIsNotSilent:
    def test_a_skipped_projection_is_counted_and_reported(self):
        _gapped_history()
        real = glicko2.rate
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:  # the first rate() call of the second match
                raise glicko2.VolatilityConvergenceError("boom")
            return real(*args, **kwargs)

        with mock.patch.object(glicko2, "rate", flaky):
            report = run_divergence(tiny_rd=1000)
        assert report.skipped_shadow == 1
        assert len(report.skipped_match_ids) == 1
        assert "skipped" in report.render().lower()
        assert report.as_dict()["skipped_shadow"] == 1
        assert report.as_dict()["skipped_match_ids"] == report.skipped_match_ids

    def test_a_clean_run_reports_zero(self):
        _gapped_history()
        report = run_divergence(tiny_rd=1000)
        assert report.skipped_shadow == 0
        assert _singles(report).no_glicko_state == 0
        assert report.warnings == []

    def test_players_left_without_glicko_state_are_counted(self):
        _gapped_history()
        with mock.patch.object(glicko2, "rate", side_effect=glicko2.Glicko2Error("x")):
            report = run_divergence(tiny_rd=1000)
        fr = _singles(report)
        assert fr.no_glicko_state == 3
        assert report.skipped_shadow == report.rated_matches
        assert fr.as_dict()["no_glicko_state"] == 3
        assert "without Glicko" in report.render()


@pytest.mark.django_db
class TestSnapshot:
    def test_a_count_mismatch_warns_instead_of_asserting(self):
        _gapped_history()
        fake = mock.Mock()
        fake.count.return_value = 999
        with mock.patch.object(
            rating_divergence, "confirmed_history", return_value=fake
        ):
            report = run_divergence()
        assert report.warnings
        assert "WARNING" in report.render()
        assert report.as_dict()["warnings"] == report.warnings

    def test_postgres_runs_in_a_repeatable_read_read_only_transaction(self):
        _gapped_history()
        fake_connection = mock.MagicMock()
        fake_connection.vendor = "postgresql"
        fake_connection.in_atomic_block = False
        with mock.patch.object(rating_divergence, "connection", fake_connection):
            run_divergence()
        sql = fake_connection.cursor.return_value.__enter__.return_value.execute
        sql.assert_called_once()
        statement = sql.call_args.args[0].upper()
        assert "REPEATABLE READ" in statement
        assert "READ ONLY" in statement

    def test_other_databases_run_plainly(self):
        _gapped_history()
        fake_connection = mock.MagicMock()
        fake_connection.vendor = "sqlite"
        with mock.patch.object(rating_divergence, "connection", fake_connection):
            run_divergence()
        fake_connection.cursor.assert_not_called()


@pytest.mark.django_db
class TestDeterminismAcrossClocks:
    def test_two_different_nows_give_identical_reports(self):
        _gapped_history()
        reports = []
        for now in (
            datetime(2030, 1, 1, tzinfo=BASE.tzinfo),
            datetime(2041, 6, 7, tzinfo=BASE.tzinfo),
        ):
            with mock.patch("django.utils.timezone.now", return_value=now):
                reports.append(run_divergence(tiny_rd=1000))
        assert reports[0].as_dict() == reports[1].as_dict()
        assert reports[0].render() == reports[1].render()


@pytest.mark.django_db
class TestEloTies:
    def test_tied_elo_is_marked_not_ranked_by_pk(self):
        a, b, c, d = (PlayerFactory(name=n) for n in ("Ada", "Bob", "Cy", "Di"))
        _play([a], [b], day=0)
        _play([c], [d], day=1)  # Ada and Cy both 1524
        report = run_divergence(tiny_rd=1000)
        fr = _singles(report)
        tied = [p for p in fr.players if p.name in ("Ada", "Cy")]
        assert all(p.elo_tied and p.rank_change is None for p in tied)
        assert not [m for m in fr.movers if m.elo_tied and m.rank_change is not None]
        assert "tie" in report.render()

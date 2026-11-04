"""S6.2 -- the pure Glicko-2 module (KAN-65).

**No `@pytest.mark.django_db` anywhere in this file, and that is the point.**
`glicko2.py` takes numbers and returns numbers: no Django, no models, no
notion of match, player or side.

The reference is Mark Glickman, "Example of the Glicko-2 system" (2012,
updated 2013), http://www.glicko.net/glicko/glicko2.pdf. Expected values are
the paper's published figures, not derived from the code under test.
"""

import itertools
import math
import subprocess
import sys
from pathlib import Path

import pytest

from pingpong import glicko2
from pingpong.glicko2 import (
    BASE_RATING,
    DEFAULT_VOLATILITY,
    MAX_RD,
    SCALE,
    Game,
    NonFiniteValueError,
    Rating,
    VolatilityConvergenceError,
    inflate,
    rate,
)

TAU = 0.5

# Glickman's worked example: player 1500 / RD 200 / sigma 0.06, tau 0.5.
PAPER_PLAYER = Rating(1500.0, 200.0, 0.06)
PAPER_GAMES = [
    Game(1400.0, 30.0, 1.0),
    Game(1550.0, 100.0, 0.0),
    Game(1700.0, 300.0, 0.0),
]


def _scaled(games):
    return [
        glicko2.ScaledGame(
            glicko2.to_mu(g.opponent_rating), glicko2.to_phi(g.opponent_rd), g.score
        )
        for g in games
    ]


class TestGlickmanWorkedExample:
    """Every figure below is printed in Glickman's paper (steps 2-8)."""

    def test_scale_conversion_step_2(self):
        assert SCALE == pytest.approx(173.7178)
        assert glicko2.to_phi(200.0) == pytest.approx(1.1513, abs=5e-5)
        assert glicko2.to_mu(1400.0) == pytest.approx(-0.5756, abs=5e-5)
        assert glicko2.to_mu(1550.0) == pytest.approx(0.2878, abs=5e-5)
        assert glicko2.to_mu(1700.0) == pytest.approx(1.1513, abs=5e-5)
        assert glicko2.to_phi(30.0) == pytest.approx(0.1727, abs=5e-5)

    def test_scale_conversions_round_trip(self):
        assert glicko2.from_mu(glicko2.to_mu(1623.4)) == pytest.approx(1623.4)
        assert glicko2.from_phi(glicko2.to_phi(87.5)) == pytest.approx(87.5)

    def test_g_step_3(self):
        phis = [glicko2.to_phi(30.0), glicko2.to_phi(100.0), glicko2.to_phi(300.0)]
        got = [glicko2.g(p) for p in phis]
        assert got == pytest.approx([0.9955, 0.9531, 0.7242], abs=5e-5)

    def test_expected_score_step_3(self):
        mu = glicko2.to_mu(1500.0)
        got = [glicko2.expected(mu, s.mu, s.phi) for s in _scaled(PAPER_GAMES)]
        assert got == pytest.approx([0.639, 0.432, 0.303], abs=5e-4)

    def test_estimated_variance_step_3(self):
        v = glicko2.estimated_variance(0.0, _scaled(PAPER_GAMES))
        # The paper carries rounded g and E (4 and 3 places) into v and Delta,
        # so its 1.7785 / -0.4834 differ from exact arithmetic (1.7790 /
        # -0.4839) in the fourth place. Hence the looser tolerance.
        assert v == pytest.approx(1.7785, abs=1e-3)

    def test_improvement_step_4(self):
        games = _scaled(PAPER_GAMES)
        v = glicko2.estimated_variance(0.0, games)
        delta = glicko2.improvement(0.0, v, games)
        assert delta == pytest.approx(-0.4834, abs=1e-3)

    def test_new_volatility_step_5(self):
        games = _scaled(PAPER_GAMES)
        v = glicko2.estimated_variance(0.0, games)
        delta = glicko2.improvement(0.0, v, games)
        sigma = glicko2.new_volatility(glicko2.to_phi(200.0), 0.06, delta, v, TAU)
        # The paper prints sigma' to 5 places (0.05999); exact arithmetic gives
        # 0.0599960. Pin it tightly: 0.06 (sigma unchanged) must fail.
        assert sigma == pytest.approx(0.0599960, abs=5e-8)

    def test_full_update_steps_6_to_8(self):
        new = rate(PAPER_PLAYER, PAPER_GAMES, tau=TAU)
        # The paper rounds to 1464.06 / 151.52; exact values pinned tighter.
        assert new.rating == pytest.approx(1464.0507, abs=1e-4)
        assert new.rd == pytest.approx(151.5165, abs=1e-4)
        assert new.volatility == pytest.approx(0.0599960, abs=5e-8)

    def test_volatility_moves_on_the_wide_bracket_branch(self):
        # A big upset takes step 5's Delta^2 > phi^2 + v branch and visibly
        # moves sigma. Reference values from an independent implementation
        # written during review (KAN-65).
        new = rate(Rating(1500.0, 50.0, 0.06), [Game(2000.0, 30.0, 1.0)], tau=1.2)
        assert new.volatility == pytest.approx(0.0600646407, abs=1e-9)
        assert new.rd == pytest.approx(50.9659378, abs=1e-6)
        assert new.rating == pytest.approx(1514.0830, abs=1e-4)

    def test_new_mu_and_phi_on_internal_scale(self):
        new = rate(PAPER_PLAYER, PAPER_GAMES, tau=TAU)
        assert glicko2.to_mu(new.rating) == pytest.approx(-0.2069, abs=5e-5)
        assert glicko2.to_phi(new.rd) == pytest.approx(0.8722, abs=5e-5)


class TestNoGames:
    def test_rating_and_volatility_are_unchanged_and_rd_grows(self):
        new = rate(Rating(1600.0, 80.0, 0.06), [], tau=TAU)
        assert new.rating == 1600.0
        assert new.volatility == 0.06
        expected = math.sqrt((80.0 / SCALE) ** 2 + 0.06**2) * SCALE
        assert new.rd == pytest.approx(expected)
        assert new.rd > 80.0

    def test_rd_is_clamped_at_rd_max(self):
        new = rate(Rating(1500.0, 349.9, 0.5), [], tau=TAU)
        assert new.rd == MAX_RD

    def test_rd_is_clamped_at_a_custom_rd_max(self):
        new = rate(Rating(1500.0, 100.0, 0.06), [], tau=TAU, rd_max=100.0)
        assert new.rd == 100.0

    def test_accepts_any_empty_sequence(self):
        assert rate(Rating(1500.0, 200.0, 0.06), (), tau=TAU).rating == 1500.0


class TestInflate:
    def test_zero_periods_is_unchanged(self):
        r = Rating(1530.0, 120.0, 0.06)
        assert inflate(r, 0.0) == r

    def test_zero_periods_still_clamps_to_rd_max(self):
        assert inflate(Rating(1500.0, 400.0, 0.06), 0.0).rd == 350.0

    def test_new_volatility_rejects_a_bad_tau(self):
        with pytest.raises(ValueError):
            glicko2.new_volatility(1.0, 0.06, 0.1, 1.0, 0.0)

    def test_matches_the_decision_formula(self):
        r = Rating(1500.0, 100.0, 0.06)
        phi = 100.0 / SCALE
        want = math.sqrt(phi**2 + 4.0 * 0.06**2) * SCALE
        got = inflate(r, 4.0)
        assert got.rd == pytest.approx(want)
        assert got.rating == 1500.0
        assert got.volatility == 0.06

    def test_one_period_equals_the_no_games_step(self):
        r = Rating(1500.0, 100.0, 0.06)
        assert inflate(r, 1.0).rd == pytest.approx(rate(r, [], tau=TAU).rd)

    def test_fractional_periods_are_allowed(self):
        r = Rating(1500.0, 100.0, 0.06)
        assert r.rd < inflate(r, 0.5).rd < inflate(r, 1.0).rd

    def test_monotonic_in_periods(self):
        r = Rating(1500.0, 50.0, 0.06)
        rds = [inflate(r, p).rd for p in (0, 1, 5, 20, 100, 1000)]
        assert rds == sorted(rds)
        assert rds[0] < rds[-1]

    def test_clamped_at_the_maximum(self):
        assert inflate(Rating(1500.0, 50.0, 0.06), 1e9).rd == MAX_RD

    def test_clamped_at_a_custom_maximum(self):
        assert inflate(Rating(1500.0, 50.0, 0.06), 1e9, rd_max=200.0).rd == 200.0

    def test_negative_periods_raise(self):
        with pytest.raises(ValueError):
            inflate(Rating(1500.0, 50.0, 0.06), -1.0)

    def test_nan_periods_raise(self):
        with pytest.raises(ValueError):
            inflate(Rating(1500.0, 50.0, 0.06), float("nan"))


class TestVolatilityConvergence:
    def test_non_convergence_raises_the_named_exception(self, monkeypatch):
        monkeypatch.setattr(glicko2, "MAX_ITERATIONS", 1)
        with pytest.raises(VolatilityConvergenceError):
            rate(PAPER_PLAYER, PAPER_GAMES, tau=TAU)

    def test_the_exception_is_never_swallowed_into_a_sigma(self, monkeypatch):
        monkeypatch.setattr(glicko2, "MAX_ITERATIONS", 0)
        with pytest.raises(VolatilityConvergenceError):
            glicko2.new_volatility(1.0, 0.06, -0.48, 1.78, TAU)

    def test_non_finite_intermediates_raise(self):
        with pytest.raises(NonFiniteValueError):
            rate(Rating(1e300, 50.0, 0.06), [Game(0.0, 50.0, 1.0)], tau=TAU)

    def test_convergence_error_is_an_arithmetic_error(self):
        assert issubclass(VolatilityConvergenceError, ArithmeticError)
        assert issubclass(NonFiniteValueError, ArithmeticError)

    def test_epsilon_is_the_papers(self):
        assert glicko2.CONVERGENCE_TOLERANCE == 0.000001


class TestEdgeCases:
    def test_a_single_game(self):
        win = rate(Rating(1500.0, 200.0, 0.06), [Game(1500.0, 200.0, 1.0)], tau=TAU)
        loss = rate(Rating(1500.0, 200.0, 0.06), [Game(1500.0, 200.0, 0.0)], tau=TAU)
        assert win.rating > 1500.0 > loss.rating
        assert win.rd < 200.0
        assert win.rating - 1500.0 == pytest.approx(1500.0 - loss.rating)

    def test_the_same_opponent_several_times(self):
        game = Game(1450.0, 80.0, 1.0)
        once = rate(PAPER_PLAYER, [game], tau=TAU)
        thrice = rate(PAPER_PLAYER, [game] * 3, tau=TAU)
        assert thrice.rating > once.rating
        assert thrice.rd < once.rd

    def test_an_opponent_at_rd_350(self):
        new = rate(Rating(1500.0, 200.0, 0.06), [Game(1500.0, 350.0, 1.0)], tau=TAU)
        assert math.isfinite(new.rating)
        assert 1500.0 < new.rating
        # An uncertain opponent moves you less than a certain one.
        sure = rate(Rating(1500.0, 200.0, 0.06), [Game(1500.0, 30.0, 1.0)], tau=TAU)
        assert new.rating < sure.rating

    def test_all_wins_and_all_losses(self):
        opponents = [(1400.0, 50.0), (1500.0, 80.0), (1650.0, 120.0)]
        wins = rate(PAPER_PLAYER, [Game(r, d, 1.0) for r, d in opponents], tau=TAU)
        losses = rate(PAPER_PLAYER, [Game(r, d, 0.0) for r, d in opponents], tau=TAU)
        assert wins.rating > 1500.0 > losses.rating
        for out in (wins, losses):
            assert all(math.isfinite(x) for x in out)

    @pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
    @pytest.mark.parametrize("pair", [(3000.0, 0.0), (0.0, 3000.0), (3000.0, 3000.0)])
    def test_extreme_ratings_give_finite_outputs(self, pair, score):
        mine, theirs = pair
        new = rate(Rating(mine, 350.0, 0.06), [Game(theirs, 350.0, score)], tau=TAU)
        assert all(math.isfinite(x) for x in new)
        assert 0 < new.rd <= MAX_RD
        assert new.volatility > 0

    def test_extreme_ratings_with_tight_rd_are_finite(self):
        new = rate(Rating(3000.0, 30.0, 0.06), [Game(0.0, 30.0, 0.0)], tau=TAU)
        assert all(math.isfinite(x) for x in new)

    def test_game_order_does_not_change_the_result(self):
        # Within one period the update is symmetric in the games, so order
        # may not change the answer beyond float summation noise.
        a = rate(PAPER_PLAYER, PAPER_GAMES, tau=TAU)
        b = rate(PAPER_PLAYER, list(reversed(PAPER_GAMES)), tau=TAU)
        assert a.rating == pytest.approx(b.rating, abs=1e-9)
        assert a.rd == pytest.approx(b.rd, abs=1e-9)

    def test_rd_is_clamped_after_a_played_period(self):
        new = rate(
            Rating(1500.0, 350.0, 1.0),
            [Game(1500.0, 350.0, 1.0)],
            tau=TAU,
            rd_max=300.0,
        )
        assert new.rd <= 300.0


class TestExpectedScore:
    def test_equal_ratings_give_a_half(self):
        assert glicko2.expected_score(1500.0, 1500.0, 80.0) == pytest.approx(0.5)

    def test_matches_the_paper_on_the_public_scale(self):
        assert glicko2.expected_score(1500.0, 1400.0, 30.0) == pytest.approx(
            0.639, abs=5e-4
        )
        assert glicko2.expected_score(1500.0, 1700.0, 300.0) == pytest.approx(
            0.303, abs=5e-4
        )

    def test_is_in_unit_interval_and_complementary_at_equal_rd(self):
        a = glicko2.expected_score(1700.0, 1400.0, 100.0)
        b = glicko2.expected_score(1400.0, 1700.0, 100.0)
        assert 0.0 < a < 1.0
        assert a + b == pytest.approx(1.0)

    def test_higher_opponent_rd_pulls_towards_a_half(self):
        sure = glicko2.expected_score(1700.0, 1400.0, 30.0)
        unsure = glicko2.expected_score(1700.0, 1400.0, 350.0)
        assert 0.5 < unsure < sure


class TestValidation:
    def test_defaults_are_the_documented_ones(self):
        assert BASE_RATING == 1500.0
        assert MAX_RD == 350.0
        assert DEFAULT_VOLATILITY == 0.06

    @pytest.mark.parametrize("rd", [0.0, -1.0, float("nan")])
    def test_rd_must_be_positive(self, rd):
        with pytest.raises(ValueError):
            rate(Rating(1500.0, rd, 0.06), [], tau=TAU)

    @pytest.mark.parametrize("sigma", [0.0, -0.06, float("nan")])
    def test_volatility_must_be_positive(self, sigma):
        with pytest.raises(ValueError):
            rate(Rating(1500.0, 200.0, sigma), [], tau=TAU)

    @pytest.mark.parametrize("tau", [0.0, -0.5, float("nan")])
    def test_tau_must_be_positive(self, tau):
        with pytest.raises(ValueError):
            rate(PAPER_PLAYER, PAPER_GAMES, tau=tau)

    @pytest.mark.parametrize("score", [-0.1, 1.1, float("nan")])
    def test_scores_must_be_in_the_unit_interval(self, score):
        with pytest.raises(ValueError):
            rate(PAPER_PLAYER, [Game(1500.0, 100.0, score)], tau=TAU)

    def test_opponent_rd_must_be_positive(self):
        with pytest.raises(ValueError):
            rate(PAPER_PLAYER, [Game(1500.0, 0.0, 1.0)], tau=TAU)

    def test_non_finite_rating_is_rejected(self):
        with pytest.raises(ValueError):
            rate(Rating(float("inf"), 200.0, 0.06), [], tau=TAU)

    def test_inflate_validates_the_rating(self):
        with pytest.raises(ValueError):
            inflate(Rating(1500.0, 0.0, 0.06), 1.0)

    def test_tau_is_required(self):
        with pytest.raises(TypeError):
            rate(PAPER_PLAYER, PAPER_GAMES)  # type: ignore[call-arg]

    def test_draw_score_is_accepted(self):
        assert rate(PAPER_PLAYER, [Game(1500.0, 100.0, 0.5)], tau=TAU)


RATINGS = [1000.0, 1350.0, 1500.0, 1700.0, 2200.0]
RDS = [30.0, 100.0, 200.0, 350.0]
VOLS = [0.03, 0.06, 0.1]


class TestProperties:
    def test_winning_never_decreases_the_rating(self):
        for r, rd, sigma, opp_r, opp_rd in itertools.product(
            RATINGS, RDS, VOLS, RATINGS, RDS
        ):
            before = Rating(r, rd, sigma)
            after = rate(before, [Game(opp_r, opp_rd, 1.0)], tau=TAU)
            assert after.rating >= before.rating, (before, opp_r, opp_rd)

    def test_losing_never_increases_the_rating(self):
        for r, rd, opp_r, opp_rd in itertools.product(RATINGS, RDS, RATINGS, RDS):
            before = Rating(r, rd, 0.06)
            after = rate(before, [Game(opp_r, opp_rd, 0.0)], tau=TAU)
            assert after.rating <= before.rating, (before, opp_r, opp_rd)

    def test_rd_never_increases_in_a_period_with_games(self):
        # Holds whenever the game carries more information than the volatility
        # step adds. A very certain player (RD 30) against an equally certain
        # opponent can tick up slightly, because phi* = sqrt(phi^2 + sigma'^2)
        # exceeds phi by more than one game removes. That is Glicko-2, not a
        # bug, so the grid is RD >= 100 with volatility <= 0.06 (Glickman's default;
        # a large sigma at RD 100 can tick up too). The bound in the next test
        # holds everywhere.
        for r, rd, sigma, opp_r, opp_rd, score in itertools.product(
            [1300.0, 1500.0, 1700.0],
            [100.0, 200.0, 350.0],
            [0.03, 0.06],
            [1300.0, 1500.0, 1700.0],
            RDS,
            [0.0, 0.5, 1.0],
        ):
            before = Rating(r, rd, sigma)
            after = rate(before, [Game(opp_r, opp_rd, score)], tau=TAU)
            assert after.rd <= before.rd + 1e-9, (before, opp_r, opp_rd, score)

    def test_rd_never_exceeds_the_inflated_rd_after_a_game(self):
        for r, rd, sigma, opp_r, opp_rd, score in itertools.product(
            RATINGS, RDS, VOLS, RATINGS, RDS, [0.0, 0.5, 1.0]
        ):
            before = Rating(r, rd, sigma)
            after = rate(before, [Game(opp_r, opp_rd, score)], tau=TAU)
            # phi' <= phi* = sqrt(phi^2 + sigma'^2); sigma' stays near sigma.
            bound = math.sqrt(rd**2 + (after.volatility * SCALE) ** 2)
            assert after.rd <= min(bound, MAX_RD) + 1e-9

    def test_draws_between_equals_leave_the_rating_unchanged(self):
        for r, rd, opp_rd in itertools.product(RATINGS, RDS, RDS):
            after = rate(Rating(r, rd, 0.06), [Game(r, opp_rd, 0.5)], tau=TAU)
            assert after.rating == pytest.approx(r, abs=1e-9)

    def test_rd_never_exceeds_rd_max(self):
        for rd, sigma in itertools.product(RDS, [0.06, 0.5, 1.0]):
            assert rate(Rating(1500.0, rd, sigma), [], tau=TAU).rd <= MAX_RD
            assert inflate(Rating(1500.0, rd, sigma), 50.0).rd <= MAX_RD


class TestDeterminism:
    def test_repeated_runs_are_bit_identical(self):
        first = rate(PAPER_PLAYER, PAPER_GAMES, tau=TAU)
        for _ in range(25):
            again = rate(PAPER_PLAYER, PAPER_GAMES, tau=TAU)
            assert again == first
            assert [x.hex() for x in again] == [x.hex() for x in first]

    def test_inputs_are_not_mutated(self):
        games = list(PAPER_GAMES)
        rate(PAPER_PLAYER, games, tau=TAU)
        assert games == PAPER_GAMES


class TestPurity:
    PATH = Path(glicko2.__file__)

    def test_imports_with_django_unavailable(self):
        code = (
            "import sys, importlib.util;"
            "sys.modules['django'] = None;"
            f"spec = importlib.util.spec_from_file_location('g', {str(self.PATH)!r});"
            "m = importlib.util.module_from_spec(spec);"
            "sys.modules['g'] = m;"
            "spec.loader.exec_module(m);"
            "print(m.rate)"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_source_imports_no_django_and_no_elo(self):
        import ast

        tree = ast.parse(self.PATH.read_text())
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        assert not [m for m in modules if m.split(".")[0] in {"django", "pingpong"}]
        assert not [m for m in modules if "elo" in m]

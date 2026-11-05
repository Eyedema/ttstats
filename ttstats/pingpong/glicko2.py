"""Pure Glicko-2 rating arithmetic (KAN-65, S6.2).

A faithful implementation of Mark Glickman, "Example of the Glicko-2 system"
(2012, updated 2013), http://www.glicko.net/glicko/glicko2.pdf. Step numbers in
the docstrings refer to that paper.

Numbers in, numbers out: no Django, no models, no notion of match, player or
side, and no import of the rating pipeline. The per-match policy (one game per rating
period, idle-time inflation, seasons) lives in the Django adapter; see
``docs/decisions/kan-63-glicko2.md``. Policy constants (tau, the rating-period
length, the season RD floor) are deliberately absent: tau is a required
parameter of :func:`rate`. Only algorithm-level defaults live here.

Everything is deterministic: games are processed in the order given, and no
set or dict ordering is involved.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

# Glickman step 2: the factor between the public (Glicko) scale and the
# internal Glicko-2 scale, 400 / ln(10).
SCALE = 173.7178

# The rating every unrated player starts from, and the centre of the scale.
BASE_RATING = 1500.0

# Default ceiling for RD on the public scale: an unrated player's RD.
MAX_RD = 350.0

# Glickman's suggested starting volatility.
DEFAULT_VOLATILITY = 0.06

# Step 5.1: the convergence tolerance (epsilon) of the volatility iteration.
CONVERGENCE_TOLERANCE = 0.000001

# Hard cap on iterations in step 5. The Illinois method converges in a
# handful of steps on any sane input; hitting the cap means something is
# wrong, and we raise rather than return a sigma that has not converged.
MAX_ITERATIONS = 100


class Glicko2Error(ArithmeticError):
    """Base class for numeric failures inside the Glicko-2 computation."""


class VolatilityConvergenceError(Glicko2Error):
    """Step 5 did not converge within ``MAX_ITERATIONS``."""


class NonFiniteValueError(Glicko2Error):
    """An intermediate value was NaN, infinite, or overflowed."""


class Rating(NamedTuple):
    """A rating on the public scale (centred on 1500)."""

    rating: float
    rd: float
    volatility: float


class Game(NamedTuple):
    """One game: the opponent's rating and RD, and our score (0, 0.5 or 1)."""

    opponent_rating: float
    opponent_rd: float
    score: float


class ScaledGame(NamedTuple):
    """A game on the internal Glicko-2 scale: (mu_j, phi_j, s_j)."""

    mu: float
    phi: float
    score: float


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise NonFiniteValueError(f"{name} is not finite: {value!r}")
    return value


def _validate_rating(rating: Rating) -> None:
    if not math.isfinite(rating.rating):
        raise ValueError(f"rating must be finite, got {rating.rating!r}")
    if not (math.isfinite(rating.rd) and rating.rd > 0):
        raise ValueError(f"rd must be positive and finite, got {rating.rd!r}")
    if not (math.isfinite(rating.volatility) and rating.volatility > 0):
        raise ValueError(
            f"volatility must be positive and finite, got {rating.volatility!r}"
        )


def _validate_rd_max(rd_max: float) -> None:
    if not (math.isfinite(rd_max) and rd_max > 0):
        raise ValueError(f"rd_max must be positive and finite, got {rd_max!r}")


def _validate_game(game: Game) -> None:
    if not math.isfinite(game.opponent_rating):
        raise ValueError(
            f"opponent rating must be finite, got {game.opponent_rating!r}"
        )
    if not (math.isfinite(game.opponent_rd) and game.opponent_rd > 0):
        raise ValueError(
            f"opponent rd must be positive and finite, got {game.opponent_rd!r}"
        )
    if not (0.0 <= game.score <= 1.0):  # also False for NaN
        raise ValueError(f"score must be in [0, 1], got {game.score!r}")


# --------------------------------------------------------------------------
# Step 2: scale conversion
# --------------------------------------------------------------------------


def to_mu(rating: float) -> float:
    """Public rating r to internal mu = (r - 1500) / SCALE (step 2)."""
    return (rating - BASE_RATING) / SCALE


def to_phi(rd: float) -> float:
    """Public RD to internal phi = RD / SCALE (step 2)."""
    return rd / SCALE


def from_mu(mu: float) -> float:
    """Internal mu back to a public rating r = SCALE * mu + 1500 (step 8)."""
    return mu * SCALE + BASE_RATING


def from_phi(phi: float) -> float:
    """Internal phi back to a public RD = SCALE * phi (step 8)."""
    return phi * SCALE


# --------------------------------------------------------------------------
# Step 3: g, E and the estimated variance
# --------------------------------------------------------------------------


def g(phi: float) -> float:
    """g(phi) = 1 / sqrt(1 + 3 phi^2 / pi^2) (step 3)."""
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def expected(mu: float, mu_j: float, phi_j: float) -> float:
    """E(mu, mu_j, phi_j) = 1 / (1 + exp(-g(phi_j)(mu - mu_j))) (step 3).

    Evaluated in the overflow-safe form, so extreme gaps saturate at 0 or 1
    rather than raising.
    """
    x = g(phi_j) * (mu - mu_j)
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def estimated_variance(mu: float, games: Sequence[ScaledGame]) -> float:
    """v = [ sum g(phi_j)^2 E (1 - E) ]^-1 (step 3)."""
    total = 0.0
    for game in games:
        gj = g(game.phi)
        e = expected(mu, game.mu, game.phi)
        total += gj * gj * e * (1.0 - e)
    if total <= 0.0 or not math.isfinite(total):
        raise NonFiniteValueError(
            "estimated variance is undefined: the games carry no information"
        )
    return _finite(1.0 / total, "estimated variance")


def improvement(mu: float, v: float, games: Sequence[ScaledGame]) -> float:
    """Delta = v * sum g(phi_j)(s_j - E) (step 4)."""
    total = 0.0
    for game in games:
        total += g(game.phi) * (game.score - expected(mu, game.mu, game.phi))
    return _finite(v * total, "improvement")


# --------------------------------------------------------------------------
# Step 5: the new volatility (Illinois algorithm)
# --------------------------------------------------------------------------


def new_volatility(
    phi: float, sigma: float, delta: float, v: float, tau: float
) -> float:
    """Solve for the new volatility sigma' (step 5).

    Uses the Illinois (regula falsi) iteration of step 5.1 to find the root of
    f(x). Raises :class:`VolatilityConvergenceError` if the bracket has not
    closed to ``CONVERGENCE_TOLERANCE`` within ``MAX_ITERATIONS`` iterations;
    it never returns an unconverged sigma. Raises :class:`NonFiniteValueError`
    on NaN, infinity or overflow.
    """
    if not (math.isfinite(tau) and tau > 0):
        raise ValueError(f"tau must be positive and finite, got {tau!r}")
    try:
        return _new_volatility(phi, sigma, delta, v, tau)
    except (OverflowError, ZeroDivisionError, ValueError) as exc:
        raise NonFiniteValueError(f"volatility update failed: {exc}") from exc


def _new_volatility(
    phi: float, sigma: float, delta: float, v: float, tau: float
) -> float:
    a = math.log(sigma * sigma)  # step 5.1
    phi2 = phi * phi
    delta2 = delta * delta

    def f(x: float) -> float:
        ex = math.exp(x)
        denom = 2.0 * (phi2 + v + ex) ** 2
        return ex * (delta2 - phi2 - v - ex) / denom - (x - a) / (tau * tau)

    # Step 5.2: set the initial bracket [A, B].
    big_a = a
    if delta2 > phi2 + v:
        big_b = math.log(delta2 - phi2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0.0:
            if k >= MAX_ITERATIONS:
                raise VolatilityConvergenceError(
                    "could not bracket the volatility root"
                )
            k += 1
        big_b = a - k * tau

    f_a = f(big_a)
    f_b = f(big_b)
    _finite(f_a, "f(A)")
    _finite(f_b, "f(B)")

    # Step 5.3 and 5.4: Illinois iteration until the bracket closes.
    iterations = 0
    while abs(big_b - big_a) > CONVERGENCE_TOLERANCE:
        if iterations >= MAX_ITERATIONS:
            raise VolatilityConvergenceError(
                f"volatility did not converge within {MAX_ITERATIONS} iterations"
            )
        iterations += 1
        big_c = big_a + (big_a - big_b) * f_a / (f_b - f_a)
        f_c = _finite(f(big_c), "f(C)")
        if f_c * f_b <= 0.0:
            big_a, f_a = big_b, f_b
        else:
            f_a /= 2.0
        big_b, f_b = big_c, f_c

    # Step 5.5
    return _finite(math.exp(big_a / 2.0), "new volatility")


# --------------------------------------------------------------------------
# Steps 6-8: the rating-period update
# --------------------------------------------------------------------------


def rate(
    rating: Rating,
    games: Sequence[Game],
    tau: float,
    rd_max: float = MAX_RD,
) -> Rating:
    """Update ``rating`` over one rating period of ``games`` (steps 1-8).

    ``tau`` is the system constant constraining volatility change and has no
    default: it is policy. With no games only step 6 applies: phi' =
    sqrt(phi^2 + sigma^2), with r and sigma unchanged. The returned RD is
    clamped to ``rd_max`` on both paths. Games are processed in the order
    given. Raises ``ValueError`` on invalid input, and a
    :class:`Glicko2Error` subclass when the arithmetic cannot produce a finite
    result (non-convergence, or E saturating at a rating gap of ~6,400):
    callers that must not fail, such as the shadow write, catch ``Glicko2Error``.
    """
    _validate_rating(rating)
    _validate_rd_max(rd_max)
    if not (math.isfinite(tau) and tau > 0):
        raise ValueError(f"tau must be positive and finite, got {tau!r}")
    for game in games:
        _validate_game(game)

    phi = to_phi(rating.rd)  # step 2
    sigma = rating.volatility

    if not games:  # step 6 only: no games played this period
        phi_new = math.sqrt(phi * phi + sigma * sigma)
        return Rating(rating.rating, min(from_phi(phi_new), rd_max), sigma)

    mu = to_mu(rating.rating)
    scaled = [
        ScaledGame(to_mu(g_.opponent_rating), to_phi(g_.opponent_rd), g_.score)
        for g_ in games
    ]

    v = estimated_variance(mu, scaled)  # step 3
    delta = improvement(mu, v, scaled)  # step 4
    sigma_new = new_volatility(phi, sigma, delta, v, tau)  # step 5

    phi_star = math.sqrt(phi * phi + sigma_new * sigma_new)  # step 6
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)  # step 7
    # step 7: mu' = mu + phi'^2 * sum g(s - E), and that sum is Delta / v.
    mu_new = mu + phi_new * phi_new * delta / v

    out = Rating(
        _finite(from_mu(mu_new), "new rating"),  # step 8
        min(_finite(from_phi(phi_new), "new rd"), rd_max),
        sigma_new,
    )
    return out


def inflate(rating: Rating, periods: float, rd_max: float = MAX_RD) -> Rating:
    """Grow RD for ``periods`` idle rating periods (generalised step 6).

    phi* = min(sqrt(phi^2 + periods * sigma^2), phi_max). ``periods`` may be
    fractional; 0 leaves the rating unchanged. r and sigma never change.
    Raises ``ValueError`` if ``periods`` is negative or not a number (callers
    clamp a negative gap to 0 first).
    """
    _validate_rating(rating)
    _validate_rd_max(rd_max)
    if not (periods >= 0.0) or math.isinf(periods):  # False for NaN
        raise ValueError(f"periods must be a finite number >= 0, got {periods!r}")
    sigma = rating.volatility
    if periods == 0.0:
        return Rating(rating.rating, min(rating.rd, rd_max), sigma)
    phi = to_phi(rating.rd)
    grown = from_phi(math.sqrt(phi * phi + periods * sigma * sigma))
    return Rating(rating.rating, min(grown, rd_max), sigma)


def expected_score(rating: float, opponent_rating: float, opponent_rd: float) -> float:
    """Expected score of ``rating`` against an opponent, public scale (step 3).

    Uses the opponent's RD through g(phi), exactly as the update does, so it
    is the probability the update itself would assume.
    """
    if not (math.isfinite(rating) and math.isfinite(opponent_rating)):
        raise ValueError("ratings must be finite")
    if not (math.isfinite(opponent_rd) and opponent_rd > 0):
        raise ValueError(f"opponent rd must be positive, got {opponent_rd!r}")
    return expected(to_mu(rating), to_mu(opponent_rating), to_phi(opponent_rd))


def win_probability(rating1: float, rd1: float, rating2: float, rd2: float) -> float:
    """Probability that player 1 beats player 2, using both players' RD.

    Glickman's prediction form: E = 1 / (1 + 10^(-(r1 - r2) / (400 sqrt(1 +
    3 q^2 (RD1^2 + RD2^2) / pi^2)))) with q = ln(10) / 400. Symmetric:
    win_probability(a, b) + win_probability(b, a) == 1. Unlike
    ``expected_score`` (the update's one-sided g(phi) form) this is the
    prediction both sides' uncertainty shrinks towards 0.5.
    """
    for name, value in (("rating", rating1), ("rating", rating2)):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
    for value in (rd1, rd2):
        if not (math.isfinite(value) and value >= 0):
            raise ValueError(f"rd must be finite and >= 0, got {value!r}")
    q = math.log(10.0) / 400.0
    denominator = math.sqrt(1.0 + 3.0 * q * q * (rd1 * rd1 + rd2 * rd2) / math.pi**2)
    return 1.0 / (1.0 + 10.0 ** (-(rating1 - rating2) / (400.0 * denominator)))

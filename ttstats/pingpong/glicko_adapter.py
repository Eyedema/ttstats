"""The Glicko-2 policy layer (KAN-67, docs/decisions/kan-63-glicko2.md).

`glicko2.py` is the pure arithmetic; this module decides how a *match* uses it:
one game per rating period, RD grown by the idle days since the player's last
rated match in that scope, and a season row seeded from the all-time one.

It is a **shadow**: the numbers are computed and stored beside the Elo ones by
`elo.apply_format_scopes`, and nothing reads them yet. Like
`elo.projected_elo_changes`, `projected_glicko_changes` is the only arithmetic,
for the live write and the replay alike, and it reads only stored state plus
`match.date_played.date()`, never "now".

Doubles are out of scope until KAN-70 and project to `None`.
"""

import math
from typing import NamedTuple

from . import glicko2, seasons
from .glicko2 import Rating


class GlickoParams(NamedTuple):
    tau: float
    period_days: float
    rd_max: float = glicko2.MAX_RD
    season_rd_floor: float = 100.0
    initial_volatility: float = glicko2.DEFAULT_VOLATILITY


# PLACEHOLDERS until S6.5 (KAN-68) fits tau, the rating-period length and the
# season RD floor against the real match history. Do not read meaning into them.
DEFAULT_PARAMS = GlickoParams(
    tau=0.5,  # PLACEHOLDER: Glickman suggests 0.3-1.2
    period_days=7,  # PLACEHOLDER: days that make one idle rating period
    season_rd_floor=100.0,  # PLACEHOLDER: minimum RD of a freshly seeded season row
)

# The fields a PlayerRating (or RatingState) carries as the shadow.
SHADOW_FIELDS = ("glicko_rating", "rd", "volatility", "last_rated_on")


class GlickoUpdate(NamedTuple):
    """One player's Glicko movement: stored state, inflated to the match day, result."""

    player_id: int
    old: Rating
    inflated: Rating
    new: Rating


class GlickoProjection(NamedTuple):
    side1_change: int
    side2_change: int
    updates: tuple

    def is_finite(self):
        """True when every resulting number is usable (finite, RD and sigma positive)."""
        for update in self.updates:
            new = update.new
            if not (
                math.isfinite(new.rating)
                and math.isfinite(new.rd)
                and math.isfinite(new.volatility)
            ):
                return False
            if new.rd <= 0 or new.volatility <= 0:
                return False
        return True


def days_to_periods(last_rated_on, match_day, params):
    """Idle rating periods between `last_rated_on` and `match_day`.

    A negative gap (a match confirmed out of date order) is clamped to 0, and
    never having been rated is 0 too: that state is already at maximum RD.
    """
    if last_rated_on is None:
        return 0.0
    gap = (match_day - last_rated_on).days
    return max(0, gap) / params.period_days


def _stored(row):
    """The Rating a row holds, or None when it has no Glicko state yet."""
    if row is None:
        return None
    if row.glicko_rating is None or row.rd is None or row.volatility is None:
        return None
    return Rating(row.glicko_rating, row.rd, row.volatility)


def _unrated(params):
    return Rating(glicko2.BASE_RATING, params.rd_max, params.initial_volatility)


def _inflated(row, stored, match_day, params):
    periods = days_to_periods(row.last_rated_on, match_day, params)
    return glicko2.inflate(stored, periods, rd_max=params.rd_max)


def _reading(book, player_id, scope, match_day, params):
    """(stored, inflated) Glicko state before this match, in `scope`."""
    all_time = book.row(player_id, None, scope.format)
    if scope.season_id is None:
        stored = _stored(all_time)
        if stored is None:
            base = _unrated(params)
            return base, base
        return stored, _inflated(all_time, stored, match_day, params)

    row = book.row(player_id, scope.season_id, scope.format)
    stored = _stored(row)
    if stored is not None:
        return stored, _inflated(row, stored, match_day, params)
    if row is not None:
        # The row exists but has no Glicko state (it predates the shadow).
        base = _unrated(params)
        return base, base

    # First season row: carry over from the all-time state as it stood before
    # this match; RD is the all-time RD inflated to the match day, with a floor.
    all_stored = _stored(all_time)
    if all_stored is None:
        seed = Rating(
            seasons.carry_over_seed_float(None),
            max(params.rd_max, params.season_rd_floor),
            params.initial_volatility,
        )
        return seed, seed
    all_inflated = _inflated(all_time, all_stored, match_day, params)
    seed = Rating(
        seasons.carry_over_seed_float(all_stored.rating),
        max(all_inflated.rd, params.season_rd_floor),
        all_inflated.volatility,
    )
    return seed, seed


def glicko_reading(book, player_id, scope, match_day, params):
    """The inflated pre-match Rating of a player in `scope` (mirrors `scope_reading`)."""
    return _reading(book, player_id, scope, match_day, params)[1]


def _rounded_change(new, old):
    """The signed whole-point change; 0 for a non-finite one, which
    `GlickoProjection.is_finite` rejects so the caller never stores it."""
    delta = new - old
    return round(delta) if math.isfinite(delta) else 0


def projected_glicko_changes(match, scope, *, book=None, params=DEFAULT_PARAMS):
    """A `GlickoProjection` for a match, calculated but not applied; None if not rated.

    None for a match with no winner, an empty side, or doubles (KAN-70). Each
    player is rated against the opponent's *inflated* rating and RD as one game.
    """
    from .elo import LiveBook
    from .models import Side

    if not match.winner_side or match.is_double:
        return None
    side1, side2 = match.sides()
    if len(side1) != 1 or len(side2) != 1:
        return None

    if book is None:
        book = LiveBook(match, scope.season)
    match_day = match.date_played
    match_day = match_day.date() if hasattr(match_day, "hour") else match_day
    p1, p2 = side1[0], side2[0]

    old1, pre1 = _reading(book, p1.pk, scope, match_day, params)
    old2, pre2 = _reading(book, p2.pk, scope, match_day, params)
    s1 = 1.0 if match.winner_side == Side.ONE else 0.0

    new1 = glicko2.rate(
        pre1, [glicko2.Game(pre2.rating, pre2.rd, s1)], params.tau, rd_max=params.rd_max
    )
    new2 = glicko2.rate(
        pre2,
        [glicko2.Game(pre1.rating, pre1.rd, 1.0 - s1)],
        params.tau,
        rd_max=params.rd_max,
    )
    return GlickoProjection(
        _rounded_change(new1.rating, old1.rating),
        _rounded_change(new2.rating, old2.rating),
        (GlickoUpdate(p1.pk, old1, pre1, new1), GlickoUpdate(p2.pk, old2, pre2, new2)),
    )


def apply_glicko(row, update, match_day):
    """Write a `GlickoUpdate` onto a rating row. `last_rated_on` never moves back."""
    row.glicko_rating = update.new.rating
    row.rd = update.new.rd
    row.volatility = update.new.volatility
    if row.last_rated_on is None or match_day > row.last_rated_on:
        row.last_rated_on = match_day

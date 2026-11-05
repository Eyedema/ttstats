"""The Glicko-2 policy layer (KAN-67, docs/decisions/kan-63-glicko2.md).

`glicko2.py` is the pure arithmetic; this module decides how a *match* uses it:
one game per rating period, RD grown by the idle days since the player's last
rated match in that scope, and a season row seeded from the all-time one.

It is a **shadow**: the numbers are computed and stored beside the Elo ones by
`elo.apply_format_scopes`, and nothing reads them yet. Like
`elo.projected_elo_changes`, `projected_glicko_changes` is the only arithmetic,
for the live write and the replay alike, and it reads only stored state plus
`match.date_played.date()`, never "now".

Doubles (KAN-70): each player is rated in a one-game period against a composite
opponent built from the other side (`composite_opponent`), with their own RD.
"""

import logging
import math
from typing import NamedTuple

from . import glicko2, seasons
from .glicko2 import Rating

logger = logging.getLogger(__name__)


class GlickoParams(NamedTuple):
    tau: float
    period_days: float
    rd_max: float = glicko2.MAX_RD
    season_rd_floor: float = 100.0
    initial_volatility: float = glicko2.DEFAULT_VOLATILITY


# Fitted in S6.5 (KAN-68); the grid and reasoning are in
# docs/decisions/kan-63-glicko2.md, "Parameters (KAN-68)". This is the one place.
DEFAULT_PARAMS = GlickoParams(
    # Unidentifiable on our data (one game per period keeps sigma at 0.0600), so
    # the mid-range of Glickman's 0.3-1.2. Revisit when the history has grown.
    tau=0.5,
    # Log-loss is flat from 3 to 90 days (within noise at n=101) and best at 3-7;
    # a week is the round value, and play comes in weekly-or-longer sessions.
    period_days=7,
    # Judgement call: a regular (RD ~70) idle for a year reaches RD ~100 under
    # these params. A season reset moves r toward 1500, which loses information,
    # so a seeded row is never more certain than that, yet far below 350.
    season_rd_floor=100.0,
)

# The fields a PlayerRating (or RatingState) carries as the shadow.
SHADOW_FIELDS = ("glicko_rating", "rd", "volatility", "last_rated_on")


def composite_opponent(readings):
    """One virtual opponent for a side: (mean rating, RMS of the RDs).

    `readings` are the side's inflated pre-match `Rating`s (anything with
    `.rating` and `.rd`), any number of them. The rating is the plain mean; the
    RD is sqrt(mean(RD^2)), so two well-known players form a predictable pair
    while one RD-350 partner raises the pair's uncertainty. Pure, no database.
    """
    readings = list(readings)
    if not readings:
        raise ValueError("a composite opponent needs at least one player")
    n = len(readings)
    rating = sum(r.rating for r in readings) / n
    rd = math.sqrt(sum(r.rd**2 for r in readings) / n)
    return rating, rd


class GlickoUpdate(NamedTuple):
    """One player's Glicko movement: stored state, inflated to the match day, result."""

    player_id: int
    old: Rating
    inflated: Rating
    new: Rating


class GlickoProjection(NamedTuple):
    """`updates` holds one GlickoUpdate per player (what the UI shows per player).

    `side1_change` / `side2_change` are per-side summaries: the rounded mean
    rating delta of that side's players (the player's own delta in singles).
    """

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
    # No Glicko state in the season scope, whether the row is missing or exists
    # with NULL fields (it predates the shadow, or its first projection
    # failed): seed it, decision 3.
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

    None for a match with no winner, an empty side, or a player on both sides
    (logged). Each player is rated against the other side's composite opponent
    (inflated ratings and RDs) as one game.
    """
    from .elo import LiveBook

    if book is None:
        book = LiveBook(match, scope.season)
    return project(match, scope, book, params)


def project(match, scope, book, params):
    """The arithmetic of `projected_glicko_changes`, **pure**: no database access.

    Everything it reads is in `book` and the match's memoized sides.
    `elo._plan_glicko` wraps exactly this in a swallow-all guard, which is
    only safe inside an atomic block while nothing here can run a query.
    """
    from .models import Side

    if not match.winner_side:
        return None
    side1, side2 = match.sides()
    if not side1 or not side2:
        return None
    ids1, ids2 = [p.pk for p in side1], [p.pk for p in side2]
    if (
        len(set(ids1)) != len(ids1)
        or len(set(ids2)) != len(ids2)
        or set(ids1) & set(ids2)
    ):
        logger.warning(
            f"Glicko-2 skipped for match {getattr(match, 'pk', None)}: "
            "a player appears more than once across the sides"
        )
        return None

    match_day = match.date_played
    match_day = match_day.date() if hasattr(match_day, "hour") else match_day

    readings = {
        side: [_reading(book, p.pk, scope, match_day, params) for p in players]
        for side, players in ((Side.ONE, side1), (Side.TWO, side2))
    }
    opponent = {
        Side.ONE: composite_opponent(pre for _, pre in readings[Side.TWO]),
        Side.TWO: composite_opponent(pre for _, pre in readings[Side.ONE]),
    }
    updates = {Side.ONE: [], Side.TWO: []}
    for side, players in ((Side.ONE, side1), (Side.TWO, side2)):
        score = 1.0 if match.winner_side == side else 0.0
        opp_rating, opp_rd = opponent[side]
        for p, (old, pre) in zip(players, readings[side]):
            new = glicko2.rate(
                pre,
                [glicko2.Game(opp_rating, opp_rd, score)],
                params.tau,
                rd_max=params.rd_max,
            )
            updates[side].append(GlickoUpdate(p.pk, old, pre, new))

    def side_change(side_updates):
        n = len(side_updates)
        return _rounded_change(
            sum(u.new.rating for u in side_updates) / n,
            sum(u.old.rating for u in side_updates) / n,
        )

    return GlickoProjection(
        side_change(updates[Side.ONE]),
        side_change(updates[Side.TWO]),
        tuple(updates[Side.ONE] + updates[Side.TWO]),
    )


def apply_glicko(row, update, match_day):
    """Write a `GlickoUpdate` onto a rating row. `last_rated_on` never moves back."""
    row.glicko_rating = update.new.rating
    row.rd = update.new.rd
    row.volatility = update.new.volatility
    if row.last_rated_on is None or match_day > row.last_rated_on:
        row.last_rated_on = match_day

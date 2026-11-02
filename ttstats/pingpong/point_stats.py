"""Pure aggregation over a match's point log (KAN-29 / S2.3).

The statistics themselves: serve and receive win rate, deuce record, comebacks
and the largest deficit overcome, longest run, lead changes, and the per-game
progression the momentum chart draws.

Pure functions only -- no Django imports, no database access, no caching. This
follows the precedent already set by ``live_scoring.py``, ``match_state.py``,
``championship_scheduling.py`` and ``player_hues.py``: the app's real rules live
in a module that can be exercised with plain pytest and no settings, and a thin
Django-touching layer sits above it. Caching and rollups are S2.4's job.

Input is plain data -- a sequence of :class:`Point` tuples -- never model
instances. That is what keeps the module DB-free, and it is also what lets
S2.4 feed it either raw rows or rebuilt ones without a second code path.

**Every rate is returned alongside the counts it came from** (:class:`Rate`),
so a caller can apply a minimum-sample rule without recomputing anything. A
"62% serve win rate" drawn from 13 points is noise presented as insight, and
the caller is the only layer that knows how it intends to present it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, NamedTuple, Sequence

from .live_scoring import WIN_POINTS, is_game_won

# Sides as plain integers, matching models.Side without importing it -- this
# module must stay importable with no Django settings configured.
SIDE_ONE = 1
SIDE_TWO = 2
SIDES = (SIDE_ONE, SIDE_TWO)

# A game is "deuce" once both sides have reached one short of the winning
# score, because from there the game can no longer end on the next point.
# Derived rather than typed, so changing WIN_POINTS moves it too.
DEUCE_THRESHOLD = WIN_POINTS - 1

# Below these sample sizes a rate is noise wearing a percentage sign. A "62%
# serve win rate" off 13 points says nothing about the player, and presenting
# it as if it did is the failure mode the epic names.
#
# **The decision (epic Open Question 2) is to SHOW the figure with an explicit
# low-confidence treatment, not to withhold it.** Withholding gives the reader
# no feedback at all and reads as a bug -- and the counts are always on screen
# anyway, so hiding the rate while showing "8 of 13" would be theatre. A
# caveat teaches; an absence does not.
#
# 50 served points is roughly three matches. Games use their own, smaller
# threshold: deuce and comeback records count games, not points.
MIN_SAMPLE_POINTS = 50
MIN_SAMPLE_GAMES = 5

# What counts as a comeback. Two points is an ordinary swing in a game to
# eleven -- it happens in most games and would make the statistic meaningless.
# Three is the smallest gap that reads as having been behind.
COMEBACK_DEFICIT = 3


def other_side(side: int) -> int:
    """The opposing side. ``SIDE_ONE`` <-> ``SIDE_TWO``."""
    return SIDE_TWO if side == SIDE_ONE else SIDE_ONE


class Point(NamedTuple):
    """One point, as the server recorded it.

    ``serving_side`` may be ``None`` for a point whose server was never
    established; such points are excluded from serve and receive denominators
    rather than guessed at.

    ``team1_score`` / ``team2_score`` are the score of ``game_number`` *after*
    this point -- so the last point of a game carries that game's final score.
    """

    game_number: int
    scoring_side: int
    serving_side: int | None
    team1_score: int
    team2_score: int


@dataclass(frozen=True)
class Rate:
    """A proportion that always carries its denominator.

    ``rate`` is 0.0 for an empty sample. Never ``None`` where a number is
    expected, and never a ``ZeroDivisionError``: a player with no live-scored
    matches is the common case, not an error.
    """

    won: int = 0
    total: int = 0

    @property
    def rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.won / self.total

    @property
    def percent(self) -> float:
        return self.rate * 100.0

    @property
    def has_sample(self) -> bool:
        return self.total > 0

    def is_confident(self, minimum: int = MIN_SAMPLE_POINTS) -> bool:
        """Whether this sample is big enough for the rate to mean anything.

        The threshold is an argument rather than a property so a caller can
        pass MIN_SAMPLE_GAMES for a game-level record; the default is the
        point-level one, which is what most callers want.
        """
        return self.total >= minimum

    def __add__(self, other: "Rate") -> "Rate":
        return Rate(self.won + other.won, self.total + other.total)


@dataclass(frozen=True)
class GameStats:
    """Point-level shape of a single game."""

    game_number: int
    team1_score: int
    team2_score: int
    complete: bool
    winner_side: int | None
    is_deuce: bool
    lead_changes: int
    # (team1, team2) after every point, starting from 0-0 so a chart has an
    # origin to draw from.
    progression: list[tuple[int, int]] = field(default_factory=list)
    # Keyed by side: the largest deficit that side faced at any point.
    largest_deficit: dict[int, int] = field(default_factory=dict)
    # Keyed by side: the longest run of consecutive points that side won.
    longest_run: dict[int, int] = field(default_factory=dict)

    @property
    def deficit_overcome(self) -> int:
        """How far behind the eventual winner was at their worst. 0 if none."""
        if self.winner_side is None:
            return 0
        return self.largest_deficit.get(self.winner_side, 0)

    @property
    def is_comeback(self) -> bool:
        return self.deficit_overcome >= COMEBACK_DEFICIT


@dataclass(frozen=True)
class SideStats:
    """One side's point-level record.

    Point-level figures (``serve``, ``receive``, ``points``, ``longest_run``)
    include every point played, an unfinished game included. Game-level figures
    (``deuce``, ``comebacks``, ``largest_deficit_overcome``) count only games
    that actually finished -- an abandoned game has no winner to credit.
    """

    side: int = SIDE_ONE
    serve: Rate = Rate()
    receive: Rate = Rate()
    points: Rate = Rate()
    games: Rate = Rate()
    deuce: Rate = Rate()
    comebacks: int = 0
    largest_deficit_overcome: int = 0
    longest_run: int = 0

    @property
    def has_sample(self) -> bool:
        return self.points.has_sample


@dataclass(frozen=True)
class MatchPointStats:
    """Everything the point log of one match yields."""

    games: list[GameStats] = field(default_factory=list)
    sides: dict[int, SideStats] = field(default_factory=dict)
    total_points: int = 0

    @property
    def has_data(self) -> bool:
        return self.total_points > 0

    def side(self, side: int) -> SideStats:
        return self.sides.get(side, SideStats(side=side))


def empty_match_stats() -> MatchPointStats:
    """The zero-state, spelled out once so callers never build it by hand."""
    return MatchPointStats(
        games=[],
        sides={s: SideStats(side=s) for s in SIDES},
        total_points=0,
    )


# ---------------------------------------------------------------------------
# Splitting and per-game shape
# ---------------------------------------------------------------------------


def split_games(points: Sequence[Point]) -> list[list[Point]]:
    """Group an ordered point sequence into per-game lists, order preserved.

    Grouped by consecutive runs of ``game_number`` rather than by sorting on
    it: the sequence is already the order the points were played, and trusting
    that is what keeps this function total on a malformed log instead of
    silently reordering one.
    """
    games: list[list[Point]] = []
    current_number = None
    for point in points:
        if point.game_number != current_number:
            games.append([])
            current_number = point.game_number
        games[-1].append(point)
    return games


def game_stats(points: Sequence[Point]) -> GameStats | None:
    """Point-level shape of one game. ``None`` for an empty sequence."""
    if not points:
        return None

    game_number = points[0].game_number
    progression: list[tuple[int, int]] = [(0, 0)]
    lead_changes = 0
    leader = 0  # sign of (team1 - team2); 0 means level

    largest_deficit = {SIDE_ONE: 0, SIDE_TWO: 0}
    longest_run = {SIDE_ONE: 0, SIDE_TWO: 0}
    run_side = None
    run_length = 0

    for point in points:
        score = (point.team1_score, point.team2_score)
        progression.append(score)

        # A lead change is the leader swapping, not a return to level: 5-4 to
        # 5-5 to 6-5 is one change, not two.
        margin = point.team1_score - point.team2_score
        sign = (margin > 0) - (margin < 0)
        if sign != 0:
            if leader != 0 and sign != leader:
                lead_changes += 1
            leader = sign

        deficit_one = point.team2_score - point.team1_score
        if deficit_one > largest_deficit[SIDE_ONE]:
            largest_deficit[SIDE_ONE] = deficit_one
        if -deficit_one > largest_deficit[SIDE_TWO]:
            largest_deficit[SIDE_TWO] = -deficit_one

        if point.scoring_side == run_side:
            run_length += 1
        else:
            run_side = point.scoring_side
            run_length = 1
        if run_length > longest_run.get(run_side, 0):
            longest_run[run_side] = run_length

    final = points[-1]
    team1_score, team2_score = final.team1_score, final.team2_score

    live_winner = is_game_won(team1_score, team2_score)
    winner_side = None
    if live_winner is not None:
        winner_side = SIDE_ONE if live_winner == "team1" else SIDE_TWO

    return GameStats(
        game_number=game_number,
        team1_score=team1_score,
        team2_score=team2_score,
        complete=winner_side is not None,
        winner_side=winner_side,
        is_deuce=min(team1_score, team2_score) >= DEUCE_THRESHOLD,
        lead_changes=lead_changes,
        progression=progression,
        largest_deficit=largest_deficit,
        longest_run=longest_run,
    )


# ---------------------------------------------------------------------------
# Per-side aggregation
# ---------------------------------------------------------------------------


def side_stats(points: Sequence[Point], games: Sequence[GameStats], side: int) -> SideStats:
    """One side's record over a whole match.

    Serve win rate is points won while serving over points served; receive win
    rate is points won while the *other* side served over points they served.
    A point with no recorded server counts toward neither -- guessing would put
    a number in the denominator that nothing supports.
    """
    served = served_won = received = received_won = 0
    won = 0

    for point in points:
        scored = point.scoring_side == side
        if scored:
            won += 1
        if point.serving_side == side:
            served += 1
            if scored:
                served_won += 1
        elif point.serving_side is not None:
            received += 1
            if scored:
                received_won += 1

    complete_games = [g for g in games if g.complete]
    games_won = sum(1 for g in complete_games if g.winner_side == side)

    deuce_games = [g for g in complete_games if g.is_deuce]
    deuce_won = sum(1 for g in deuce_games if g.winner_side == side)

    comebacks = sum(
        1 for g in complete_games if g.winner_side == side and g.is_comeback
    )
    largest_deficit_overcome = max(
        (g.deficit_overcome for g in complete_games if g.winner_side == side),
        default=0,
    )
    longest_run = max((g.longest_run.get(side, 0) for g in games), default=0)

    return SideStats(
        side=side,
        serve=Rate(served_won, served),
        receive=Rate(received_won, received),
        points=Rate(won, len(points)),
        games=Rate(games_won, len(complete_games)),
        deuce=Rate(deuce_won, len(deuce_games)),
        comebacks=comebacks,
        largest_deficit_overcome=largest_deficit_overcome,
        longest_run=longest_run,
    )


def match_stats(points: Sequence[Point]) -> MatchPointStats:
    """Everything one match's point log yields. Total on an empty sequence."""
    points = list(points)
    if not points:
        return empty_match_stats()

    games = [g for g in (game_stats(p) for p in split_games(points)) if g is not None]
    return MatchPointStats(
        games=games,
        sides={s: side_stats(points, games, s) for s in SIDES},
        total_points=len(points),
    )


# ---------------------------------------------------------------------------
# Combining across matches
# ---------------------------------------------------------------------------


def combine_side_stats(items: Iterable[SideStats], *, side: int = SIDE_ONE) -> SideStats:
    """Fold per-match records into a career (or head-to-head) record.

    Rates add by summing numerator and denominator -- never by averaging the
    rates, which would weight a three-point match the same as a five-game one.
    Records of a single best (longest run, largest deficit) take the maximum;
    they are not additive and summing them would invent a run nobody played.

    This is what makes S2.4's rollups sufficient: a career figure is this fold
    over a few hundred rollup rows, not a scan of every point ever played.
    """
    total = SideStats(side=side)
    for item in items:
        total = SideStats(
            side=side,
            serve=total.serve + item.serve,
            receive=total.receive + item.receive,
            points=total.points + item.points,
            games=total.games + item.games,
            deuce=total.deuce + item.deuce,
            comebacks=total.comebacks + item.comebacks,
            largest_deficit_overcome=max(
                total.largest_deficit_overcome, item.largest_deficit_overcome
            ),
            longest_run=max(total.longest_run, item.longest_run),
        )
    return total

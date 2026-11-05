"""The rating write path: Glicko-2, one system, one write (KAN-73).

`glicko_adapter.projected_glicko_changes` / `project` is the only arithmetic.
This module decides *where* it applies and writes it:

* A match rates its **format scopes**: the all-time row in its format, plus
  its season row when the match falls in a season (`format_scopes`).
* `apply_format_scopes` is the one step shared by the live write (`LiveBook`)
  and the replay (`rating_replay.MemoryBook`): every projection is planned
  before any row changes, so a season seed sees the all-time state as it was
  before this match.
* `apply_match_ratings` is the signal-path entry point: guards, then one
  atomic block per match, race-safe through the unique `RatingChange
  (match, rating)` constraint.

Elo was retired by KAN-73. `EloHistory` and `RatingChange.elo_*` are frozen
history; nothing here reads or writes them.
"""

import logging
from typing import NamedTuple

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import glicko_adapter

logger = logging.getLogger(__name__)

SINGLES = "singles"
DOUBLES = "doubles"


class RatingScope(NamedTuple):
    """Where a rating lives: a season (None = all-time) and a format."""

    season: object
    format: str

    @property
    def season_id(self):
        return getattr(self.season, "pk", self.season)


def match_format(match):
    return DOUBLES if match.is_double else SINGLES


# ---------------------------------------------------------------------------
# Rating books: where a format scope's state is read from and written to.
# A book maps (player_id, season_id, format) to a row with `matches` and the
# Glicko fields -- a PlayerRating (LiveBook) or a RatingState (replay).
# ---------------------------------------------------------------------------


class RatingState:
    """An in-memory rating row, shaped like PlayerRating."""

    __slots__ = (
        "player_id", "club_id", "season_id", "format", "matches",
        "glicko_rating", "rd", "volatility", "last_rated_on",
    )

    def __init__(self, player_id, club_id, season_id, format):
        self.player_id, self.club_id = player_id, club_id
        self.season_id, self.format = season_id, format
        self.matches = 0
        self.glicko_rating = self.rd = self.volatility = self.last_rated_on = None

    @property
    def key(self):
        return (self.player_id, self.season_id, self.format)


class RatingBook:
    """Rows keyed by (player_id, season_id, format). Subclasses supply `new_row`."""

    def __init__(self):
        self.rows = {}

    def row(self, player_id, season_id, fmt):
        return self.rows.get((player_id, season_id, fmt))

    def new_row(self, player, scope):  # pragma: no cover - abstract
        raise NotImplementedError


class FormatChange(NamedTuple):
    """One player's movement in one format scope in one match.

    `glicko` is the `GlickoUpdate` applied, or None when the projection was
    skipped (the row still counts the match; its history columns stay NULL).
    """

    row: object
    player_id: int
    glicko: object = None


def format_scopes(match, season):
    """The format scopes a match rates: all-time, plus its season if it has one."""
    fmt = match_format(match)
    scopes = [RatingScope(None, fmt)]
    if season is not None:
        scopes.append(RatingScope(season, fmt))
    return scopes


def guarded_projection(match, scope, book, params=glicko_adapter.DEFAULT_PARAMS):
    """The Glicko projection for one scope, or None to skip it.

    The only guarded step of the pipeline: a failure (a `Glicko2Error`, a
    non-finite result) is logged and costs this scope's update for this match,
    never the rest of the write. The preview (`ratings.py`) calls this same
    function, so preview == written value, including the skip.
    """
    if book is None:
        # Outside the guard on purpose: building a book queries the database,
        # and a swallowed query error would poison the surrounding atomic block.
        raise ValueError("guarded_projection needs a book; the guarded region must be pure")
    try:
        projection = glicko_adapter.project(match, scope, book, params)
        if projection is not None and not projection.is_finite():
            raise ValueError(f"non-finite Glicko result: {projection!r}")
        return projection
    except Exception:  # Glicko2Error is the expected case; any other is handled the same
        logger.exception(
            f"Glicko-2 skipped for match {getattr(match, 'pk', None)} in {scope}"
        )
        return None


def apply_format_scopes(match, book, season, *, params=glicko_adapter.DEFAULT_PARAMS):
    """Project and apply every format scope of `match` to `book`, in memory.

    The shared step of the live write and the replay. All projections are
    planned **before** any row changes. Every player on both sides gets a row
    in every scope (created if missing) and `matches += 1`; the Glicko update
    is applied where the projection succeeded.
    """
    team1_players, team2_players = match.sides()
    scopes = format_scopes(match, season)
    plan = {scope: guarded_projection(match, scope, book, params) for scope in scopes}

    match_day = match.date_played
    match_day = match_day.date() if hasattr(match_day, "hour") else match_day

    changes = []
    for scope in scopes:
        updates = {u.player_id: u for u in plan[scope].updates} if plan[scope] else {}
        for p in team1_players + team2_players:
            row = book.row(p.pk, scope.season_id, scope.format)
            if row is None:
                row = book.new_row(p, scope)
            row.matches += 1
            update = updates.get(p.pk)
            if update is not None:
                glicko_adapter.apply_glicko(row, update, match_day)
            changes.append(FormatChange(row, p.pk, update))
    return changes


class LiveBook(RatingBook):
    """PlayerRating rows for one match's players, loaded in one query.

    `lock=True` (inside `apply_match_ratings`'s transaction) takes the rows
    `select_for_update`, so two different matches for the same player
    serialise on PostgreSQL rather than losing an update.
    """

    def __init__(self, match, season=None, *, lock=False):
        super().__init__()
        self.season = season
        self.created = []
        side1, side2 = match.sides()
        self._load([p.pk for p in side1 + side2], match_format(match), season, lock)

    @classmethod
    def for_players(cls, player_ids, season, fmt):
        """A read-only book for these players, without a match (KAN-71).

        Same rows, same single query as the match constructor; used to read a
        Glicko state for a win-probability on a scheduled or hypothetical
        pairing. Never `flush` it.
        """
        book = cls.__new__(cls)
        RatingBook.__init__(book)
        book.season = season
        book.created = []
        book._load(list(player_ids), fmt, season, False)
        return book

    def _load(self, ids, fmt, season, lock):
        from .models import PlayerRating

        season_q = Q(season__isnull=True)
        if season is not None:
            season_q |= Q(season=season)
        # all_objects: filtered by these players, so transitively club-scoped
        # (KAN-24 audit), and a write must never be hidden by the viewer's
        # active club.
        qs = PlayerRating.all_objects.filter(season_q, player_id__in=ids, format=fmt)
        if lock:
            qs = qs.select_for_update()
        for r in qs:
            self.rows[(r.player_id, r.season_id, r.format)] = r

    def new_row(self, player, scope):
        from .models import PlayerRating

        row = PlayerRating(
            player_id=player.pk,
            club_id=scope.season.club_id if scope.season is not None else player.club_id,
            season=scope.season,
            format=scope.format,
            matches=0,
        )
        self.rows[(player.pk, scope.season_id, scope.format)] = row
        self.created.append(row)
        return row

    def flush(self, match, changes):
        """Persist rows and RatingChange audit rows: three queries at most."""
        from .models import PlayerRating, RatingChange

        seen = {id(r) for r in self.created}
        if self.created:
            PlayerRating.all_objects.bulk_create(self.created)
        now = timezone.now()
        existing = []
        for change in changes:
            if id(change.row) not in seen:
                seen.add(id(change.row))
                change.row.updated_at = now
                existing.append(change.row)
        if existing:
            PlayerRating.all_objects.bulk_update(
                existing, ["matches", "updated_at", *glicko_adapter.SHADOW_FIELDS]
            )
        RatingChange.objects.bulk_create([
            RatingChange(
                match_id=match.pk,
                player_id=c.player_id,
                rating=c.row,
                **glicko_adapter.history_values(c.glicko),
            )
            for c in changes
        ])


def already_applied(match):
    """Have ratings been written for this match? The idempotency guard.

    A `RatingChange` row is the answer for every match rated since KAN-47. A
    frozen `EloHistory` row also counts: a match rated under Elo that somehow
    never got rating rows (say, a database migrated past the 0040 backfill after
    it became a no-op) must not be rated a second time on top of today's
    ratings when something later touches it. `recalculate_ratings` is how such
    a match gets its Glicko rows.
    """
    from .models import EloHistory, RatingChange

    return (
        RatingChange.objects.filter(match=match).exists()
        or EloHistory.objects.filter(match=match).exists()
    )


def apply_match_ratings(match):
    """Rate a confirmed match: every format scope, in one transaction.

    Called from the completion and confirmation signals; safe to call any time
    (it guards on a winner, on confirmation and on `already_applied`).

    **Race safety.** Two confirmers can both pass the guard. The unique
    constraint on `RatingChange (match, rating)` then rolls the loser's whole
    block back, and the loser treats that as "already applied". A conflict that
    is *not* this match being applied (two matches creating the same new rating
    row) is retried once.
    """
    if not match.winner_side:
        logger.debug(f"Skipping ratings for match {match.pk}: no winner")
        return

    if not match.match_confirmed:
        logger.debug(
            f"Skipping ratings for match {match.pk}: not confirmed "
            f"(side1={match.team1_confirmed}, side2={match.team2_confirmed})"
        )
        return

    if already_applied(match):
        logger.debug(f"Skipping ratings for match {match.pk}: already applied")
        return

    for attempt in (1, 2):
        try:
            _apply_match(match)
            return
        except IntegrityError:
            match.invalidate_participant_cache()
            if already_applied(match):
                logger.info(
                    f"Ratings for match {match.pk} were applied by a concurrent confirmer"
                )
                return
            if attempt == 2:
                raise
            logger.warning(f"Rating write conflict on match {match.pk}; retrying once")


def _lock_sides(match):
    """Reload the match's players with `SELECT ... FOR UPDATE`; reset the memo.

    Must run inside the transaction. Locking the Player rows serialises two
    confirmations that share a player even when neither has a PlayerRating row
    yet (so `LiveBook`'s row lock has nothing to hold). Rows are locked in
    player-pk order so they cannot deadlock; the sides are then rebuilt in
    participant order, which is the order `sides()` and the replay use.

    Same select_related as `Match.sides()`, so the rest of the completion
    path pays no extra queries; `of=("player",)` locks only the Player rows
    (PostgreSQL refuses FOR UPDATE on the nullable side of the user joins).
    """
    from .models import MatchParticipant, Side

    participants = list(
        MatchParticipant.objects.filter(match_id=match.pk)
        .select_related("player", "player__club", "player__user", "player__user__profile")
        .select_for_update(of=("player",))
        .order_by("player_id")
    )
    participants.sort(key=lambda mp: mp.pk)
    side1 = [mp.player for mp in participants if mp.side == Side.ONE]
    side2 = [mp.player for mp in participants if mp.side != Side.ONE]
    match.invalidate_participant_cache()
    match._sides_memo = (side1, side2)
    return side1, side2


def _apply_match(match):
    """Every scope for one match, atomically. Raises IntegrityError on a race."""
    from .models import Season

    with transaction.atomic():
        team1_players, team2_players = _lock_sides(match)

        # An empty side means there is nothing to rate, singles or doubles.
        if not team1_players or not team2_players:
            logger.error(f"Skipping ratings for match {match.pk}: empty side found")
            return

        season = Season.for_day(match.club_id, match.date_played)
        book = LiveBook(match, season, lock=True)
        changes = apply_format_scopes(match, book, season)
        book.flush(match, changes)

    logger.info(
        f"Ratings updated for match {match.pk} "
        f"({'2v2' if match.is_double else '1v1'}, {len(changes)} scope rows)"
    )

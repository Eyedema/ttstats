"""Applying one live-scoring op to a locked match (KAN-87).

The single place that turns a scorekeeper's op -- ``start``, ``point``,
``undo``, ``side_switch`` -- into writes. The four single-op views and the
batch ``live_sync`` endpoint are thin wrappers around :func:`apply_op`, so a
point synced in a batch after a dead spell is applied by exactly the code that
applies a point tapped online.

This is the Django layer. The rules (who serves, when a game ends, what undo
restores) still come only from the pure ``live_scoring`` module.

**The caller owns the transaction and the lock.** ``apply_op`` expects the
match row to be locked with ``select_for_update()`` and refreshed, and the op
to have passed the sequence-number dedupe already (``views._check_seq``).
Consuming the op's ``seq`` is part of applying it: an applied op and a rule
rejection both use it up (the client drops a rejected op and carries on with
the next number); an op refused because the match is not live does not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from typing import Any, Callable

from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from . import live_scoring, point_rollups
from .models import Game, Match, PointEvent, Side

logger = logging.getLogger(__name__)

OP_TYPES = ("start", "point", "undo", "side_switch")

NOT_LIVE = "Match is not live"
NOT_STARTED = "Match has not started — pick an initial server"
ALREADY_STARTED = "Match already started"


@dataclass(frozen=True)
class OpResult:
    """What applying one op did.

    ``applied`` is False for a rejection, with ``error`` and ``status`` (409)
    saying why. ``redirect_url`` is set only when this op completed the match,
    which ``match_completed`` also says.
    """

    applied: bool
    error: str | None = None
    status: int = 200
    redirect_url: str | None = None
    match_completed: bool = False


def parse_client_ts(value: Any, *, match_day: date, now: datetime) -> datetime | None:
    """The op's ``client_ts`` as an aware UTC datetime, or ``None`` (KAN-89).

    ISO-8601 only; a naive value is taken as UTC. Clamped to
    ``[match day - 1 day, now]``: a phone's clock can be wrong, but a point
    cannot have been played in the future, nor long before the match.
    Anything unparseable is ``None`` -- a bad clock must never cost a point.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    parsed = parsed.astimezone(dt_timezone.utc)
    earliest = datetime.combine(
        match_day - timedelta(days=1), time.min, tzinfo=dt_timezone.utc
    )
    return min(max(parsed, earliest), now)


def _client_recorded_at(match: Match, op: dict[str, Any]) -> datetime | None:
    if op.get("client_ts") is None:
        return None
    return parse_client_ts(
        op["client_ts"],
        match_day=match.date_played.astimezone(dt_timezone.utc).date(),
        now=timezone.now(),
    )


def record_point_event(
    match, *, state_before, side, completed, new_state, client_recorded_at=None
):
    """Persist one ``PointEvent`` for a point the server has just applied.

    Called from inside the op's transaction, with the match row already
    locked, so sequence allocation is serialised: two taps arriving while an
    earlier request is still in flight are applied -- and numbered -- in the
    order the server took them.

    **Scoring wins over recording.** The write runs in its own savepoint and
    swallows everything: a point that reached the server must count on the
    scoreboard even if its history row cannot be stored. The savepoint is what
    makes that true on Postgres, where an error inside an atomic block poisons
    the whole transaction and would otherwise take the point down with it. A
    dropped row leaves a gap, which the fidelity harness is built to notice.

    ``state_before`` is passed in because the serving side must be read from
    the state *before* the point: afterwards ``current_server()`` names whoever
    serves next, and that mistake is invisible in the stored data.
    """
    try:
        with transaction.atomic():
            serving = live_scoring.current_server(state_before)
            if completed is not None:
                # live_state has already reset the in-progress score to 0-0,
                # so the score after this point is the completed game's final.
                team1_score = completed["team1_score"]
                team2_score = completed["team2_score"]
            else:
                team1_score = new_state["team1_points"]
                team2_score = new_state["team2_points"]

            next_sequence = (
                PointEvent.objects.filter(match=match).aggregate(
                    last=Max("sequence")
                )["last"] or 0
            ) + 1

            PointEvent.objects.create(
                match=match,
                game_number=state_before["current_game_number"],
                scoring_side=Side.from_live(side),
                serving_side=Side.from_live(serving),
                team1_score=team1_score,
                team2_score=team2_score,
                sequence=next_sequence,
                client_recorded_at=client_recorded_at,
            )
    except Exception:
        logger.exception(
            "Failed to persist PointEvent for match %s; the point still counts",
            match.pk,
        )


def apply_op(
    match: Match,
    op: dict[str, Any],
    *,
    record_point: Callable[..., None] | None = None,
) -> OpResult:
    """Apply one validated op to a locked, refreshed match.

    ``op`` is ``{"type": ..., "seq": int | None, ...}`` with the type's own
    field (``initial_server`` for start, ``side`` for point) already checked.
    A point may carry ``client_ts``; it is parsed here and never rejected.
    ``record_point`` defaults to :func:`record_point_event`.
    """
    if op["type"] == "point":
        return _point(match, op, record_point or record_point_event)
    return _HANDLERS[op["type"]](match, op)


# --- seq bookkeeping ----------------------------------------------------------


def _consume(match: Match, op: dict[str, Any]) -> dict[str, Any]:
    """Mark the op's seq used, in memory; returns the column to write.

    In memory first because ``Game.objects.create`` saves this very instance,
    every field included, and must not write the previous value back.
    """
    seq = op.get("seq")
    if seq is None:
        return {}
    match.live_applied_seq = seq
    return {"live_applied_seq": seq}


def _reject(match: Match, op: dict[str, Any], error: str) -> OpResult:
    """A rule rejection: nothing applied, but the seq is used up."""
    fields = _consume(match, op)
    if fields:
        Match.all_objects.filter(pk=match.pk).update(**fields)
    return OpResult(applied=False, error=error, status=409)


def _not_live() -> OpResult:
    return OpResult(applied=False, error=NOT_LIVE, status=409)


def _write_state(match: Match, op: dict[str, Any], new_state: dict | None, **extra) -> None:
    Match.all_objects.filter(pk=match.pk).update(
        live_state=new_state, **_consume(match, op), **extra
    )
    match.live_state = new_state


# --- the four ops -------------------------------------------------------------


def _start(match: Match, op: dict[str, Any]) -> OpResult:
    if not match.is_live:
        return _not_live()
    current = match.live_state or live_scoring.initial_state(match.best_of)
    if current.get("started"):
        return _reject(match, op, ALREADY_STARTED)
    new_state = live_scoring.set_initial_server(current, op["initial_server"])
    _write_state(match, op, new_state)
    return OpResult(applied=True)


def _point(match: Match, op: dict[str, Any], record: Callable[..., None]) -> OpResult:
    if not match.is_live:
        return _not_live()
    if not match.live_state or not match.live_state.get("started"):
        return _reject(match, op, NOT_STARTED)

    side = op["side"]
    # Captured before apply_point: current_server() on the new state names
    # who serves *next*, not who served this point.
    state_before = match.live_state
    try:
        new_state, completed = live_scoring.apply_point(state_before, side)
    except ValueError as exc:
        return _reject(match, op, str(exc))

    record(
        match,
        state_before=state_before,
        side=side,
        completed=completed,
        new_state=new_state,
        client_recorded_at=_client_recorded_at(match, op),
    )

    if completed is None:
        _write_state(match, op, new_state)
        return OpResult(applied=True)

    match_complete = live_scoring.is_match_complete(new_state)
    redirect_url = None
    if match_complete:
        # Flip is_live OFF first so the upcoming Game.save() → Match.save()
        # pipeline runs the existing winner / signal / rating cascade as if it
        # were a normal match.
        _write_state(match, op, None, is_live=False)
        match.is_live = False
        # Hand off to the existing match_confirm flow: running the live
        # scoreboard implicitly attests the score, so the scorekeeper is
        # confirmed in one step and lands on match_detail. The other player
        # still confirms via the email they receive from the post-save signal.
        redirect_url = reverse("pingpong:match_confirm", args=[match.pk])
    else:
        _write_state(match, op, new_state)

    # Persist the completed Game row. While is_live=True (mid-match) this
    # triggers Match.save() but the winner-detection guard short-circuits. On
    # the final game, is_live=False so the normal signal pipeline runs.
    Game.objects.create(
        match=match,
        game_number=completed["game_number"],
        team1_score=completed["team1_score"],
        team2_score=completed["team2_score"],
    )

    if match_complete:
        # Roll the point log up now, inside the same transaction, so the
        # summary and the games it describes commit together. This is the
        # ONLY moment a match's log stops changing on its own, so it is the
        # natural write point -- and doing it here rather than in a post_save
        # receiver keeps it out of the completion pipeline the epic measured
        # down to 39 queries. Non-fatal for the same reason the point write
        # is: a missing rollup is rebuildable from the raw log by
        # `manage.py rebuild_point_rollups`, a lost match is not.
        try:
            with transaction.atomic():
                point_rollups.rebuild_match_summaries(match)
        except Exception:
            logger.exception(
                "Failed to roll up point stats for match %s; "
                "rebuild with manage.py rebuild_point_rollups",
                match.pk,
            )

    return OpResult(
        applied=True, redirect_url=redirect_url, match_completed=match_complete
    )


def _undo(match: Match, op: dict[str, Any]) -> OpResult:
    """Undo the most recent point (KAN-10).

    If the undone point ended a game, the trailing Game row is deleted and
    the in-progress game state is restored from the event log.
    """
    if not match.is_live or not match.live_state:
        return _not_live()

    # Whether there was anything to undo at all. undo_last_point() is a no-op
    # on an empty log and returns the state unchanged, and the persisted row
    # must not be dropped in that case -- live_state's event list is capped at
    # MAX_EVENTS, so on a long match it can run out while the persisted log
    # still holds every earlier point.
    had_events = bool((match.live_state or {}).get("events"))

    new_state, undone_game = live_scoring.undo_last_point(match.live_state)

    if had_events:
        # Delete the row; never append a compensating "undone" event. A
        # tombstone would mean every aggregation query has to filter it out,
        # and the first one that forgets double-counts -- with no error and
        # nothing to notice by inspection. Deleting the trailing row also
        # keeps `sequence` gapless, which is what makes "no gaps" usable as an
        # integrity check.
        #
        # Unlike the write path, this is NOT best-effort. A failed undo rolls
        # back and the scorekeeper taps again; a log that keeps a point the
        # umpire retracted is silently wrong forever.
        trailing = (
            PointEvent.objects.filter(match=match).order_by("-sequence").first()
        )
        if trailing is not None:
            trailing.delete()

    if undone_game is not None:
        # Drop the trailing Game row. Use all_objects to bypass the live-match
        # GameManager filter.
        Game.all_objects.filter(
            match=match,
            game_number=undone_game["game_number"],
        ).delete()

    _write_state(match, op, new_state)

    if had_events:
        # A match cannot normally be undone after it completes (it is no
        # longer live), so there is usually no rollup yet. The check is one
        # indexed existence query and it is what makes a rebuilt-then-
        # corrected match self-heal rather than keeping a summary of a log
        # that no longer says that.
        point_rollups.invalidate_for_match(match)

    return OpResult(applied=True)


def _side_switch(match: Match, op: dict[str, Any]) -> OpResult:
    """Mark the deciding-game side switch as confirmed (KAN-9)."""
    if not match.is_live or not match.live_state:
        return _not_live()
    _write_state(match, op, live_scoring.confirm_side_switch(match.live_state))
    return OpResult(applied=True)


_HANDLERS: dict[str, Callable[..., OpResult]] = {
    "start": _start,
    "undo": _undo,
    "side_switch": _side_switch,
}

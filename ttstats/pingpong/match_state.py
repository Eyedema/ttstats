"""Pure rules for a persisted match's derived state.

Winner detection, score caching and confirmation status were computed in three
different places (``Match.save``, ``Match.update_cache_fields`` and the
``.update(is_confirmed=...)`` escape hatches in ``signals.py``), which is why
keeping them in sync needed a documented warning. The rules live here as pure
functions -- no Django imports, no DB access -- in the same spirit as
``live_scoring``; ``Match.recompute()`` is the single place that applies them.
"""
from __future__ import annotations

from .live_scoring import games_to_win

__all__ = [
    "games_to_win",
    "winner_side",
    "side_confirmed",
    "confirmation_complete",
    "should_auto_confirm",
]

SIDE_1 = 1
SIDE_2 = 2


def winner_side(side1_game_wins: int, side2_game_wins: int, best_of: int) -> int | None:
    """Which side has won the match, or None if neither has yet.

    Returning None means "undecided", not "nobody won" -- callers that are
    refreshing an already-finished match decide for themselves whether an
    undecided result should clear a previously recorded winner.
    """
    needed = games_to_win(best_of)
    if side1_game_wins >= needed:
        return SIDE_1
    if side2_game_wins >= needed:
        return SIDE_2
    return None


def side_confirmed(verified_player_ids, confirmed_player_ids) -> bool:
    """True when every verified player on a side has confirmed.

    A side with no verified players is vacuously confirmed: unverified players
    are never asked to confirm, so waiting on them would deadlock the match.
    """
    return set(verified_player_ids) <= set(confirmed_player_ids)


def confirmation_complete(
    side1_verified_ids, side2_verified_ids, confirmed_player_ids
) -> bool:
    """True when both sides are fully confirmed."""
    confirmed = set(confirmed_player_ids)
    return side_confirmed(side1_verified_ids, confirmed) and side_confirmed(
        side2_verified_ids, confirmed
    )


def should_auto_confirm(
    *,
    has_winner: bool,
    already_confirmed: bool,
    side1_has_verified: bool,
    side2_has_verified: bool,
) -> bool:
    """Whether a finished match should be confirmed without asking anyone.

    Only meaningful when exactly one side is entirely unverified. If *both*
    sides are unverified the match already counts as confirmed, so this
    returns False -- the caller still has to persist that state, which is the
    trap the old three-way split kept falling into.
    """
    if not has_winner or already_confirmed:
        return False
    return not side1_has_verified or not side2_has_verified

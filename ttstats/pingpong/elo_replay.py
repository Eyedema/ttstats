"""One deterministic traversal of the confirmed match history.

Elo is path-dependent, so anything that recomputes ratings from scratch must
walk the history in exactly the same order or two honest runs legitimately
disagree. This module is that walk, written once: the parity harness
(`elo_parity`) and `recalculate_elo` both read it rather than each keeping a
copy.

* **Scope:** matches with a winner **and** `is_confirmed=True`, not live.
  Championship matches can have a winner while unconfirmed; they are out, which
  is how `update_player_elo` already behaves.
* **Order:** `REPLAY_ORDER` -- `date_played`, then `created_at`, then `pk` so a
  tie on both timestamps still has exactly one answer.
* **Memory:** matches stream through `.iterator(chunk_size=...)` and their
  participants are fetched one chunk at a time. Nothing materialises the
  whole history (the VPS has 3.7 GB).

Records are light stand-ins that quack like a `Match` as far as
`elo.projected_elo_changes` cares (`winner_side`, `match_type`, `best_of`,
`is_double`, `sides()`), so the replay runs the *real* arithmetic rather than a
copy of it.
"""
from dataclasses import dataclass, field
from typing import NamedTuple

REPLAY_ORDER = ("date_played", "created_at", "pk")
DEFAULT_CHUNK_SIZE = 500


@dataclass
class ReplayPlayer:
    """A player as the arithmetic sees one: a pk plus the legacy fields."""

    pk: int
    elo_rating: int = 1500
    elo_peak: int = 1500
    matches_for_elo: int = 0


@dataclass
class ReplayMatch:
    pk: int
    date_played: object
    created_at: object
    match_type: str
    best_of: int
    is_double: bool
    winner_side: int
    club_id: object
    side1_ids: tuple = ()
    side2_ids: tuple = ()
    _players: tuple = field(default=None, repr=False)

    def bind(self, lookup):
        """Attach player stand-ins from `lookup(pk)`; returns self."""
        self._players = (
            [lookup(pk) for pk in self.side1_ids],
            [lookup(pk) for pk in self.side2_ids],
        )
        return self

    def sides(self):
        if self._players is None:
            return (
                [ReplayPlayer(pk) for pk in self.side1_ids],
                [ReplayPlayer(pk) for pk in self.side2_ids],
            )
        return self._players


def confirmed_history(match_manager):
    """The in-scope matches, ordered. Takes a manager so migrations can pass a
    historical model's."""
    return match_manager.filter(
        winner_side__isnull=False, is_confirmed=True, is_live=False
    ).order_by(*REPLAY_ORDER)


def iter_replay_matches(
    *, chunk_size=DEFAULT_CHUNK_SIZE, match_model=None, participant_model=None
):
    """Yield a `ReplayMatch` per in-scope match, in replay order.

    `match_model`/`participant_model` default to the current models; a data
    migration passes its historical ones.
    """
    if match_model is None or participant_model is None:
        from .models import Match, MatchParticipant

        match_model = match_model or Match
        participant_model = participant_model or MatchParticipant

    # `_base_manager` is never row-filtered: a replay with no viewer must see
    # every match, and historical models have no custom managers anyway.
    rows = confirmed_history(match_model._base_manager).values_list(
        "pk", "date_played", "created_at", "match_type", "best_of",
        "is_double", "winner_side", "club_id",
    ).iterator(chunk_size=chunk_size)

    chunk = []
    for row in rows:
        chunk.append(ReplayMatch(*row))
        if len(chunk) >= chunk_size:
            yield from _with_participants(chunk, participant_model)
            chunk = []
    if chunk:
        yield from _with_participants(chunk, participant_model)


def _with_participants(chunk, participant_model):
    by_pk = {m.pk: ([], []) for m in chunk}
    participants = participant_model._base_manager.filter(
        match_id__in=list(by_pk)
    ).order_by("pk").values_list("match_id", "player_id", "side")
    for match_id, player_id, side in participants:
        by_pk[match_id][0 if side == 1 else 1].append(player_id)
    for m in chunk:
        side1, side2 = by_pk[m.pk]
        m.side1_ids, m.side2_ids = tuple(side1), tuple(side2)
        yield m


class Change(NamedTuple):
    """One player's movement in one match."""

    player_id: int
    old: int
    new: int
    change: int
    k: float


class LegacyReplay:
    """Replays history through `elo.projected_elo_changes` in memory.

    Mirrors `update_player_elo`'s legacy write exactly: same guards (an empty
    side changes nothing), same arithmetic call, same peak and count updates.
    """

    def __init__(self):
        self.players = {}

    def player(self, pk):
        if pk not in self.players:
            self.players[pk] = ReplayPlayer(pk)
        return self.players[pk]

    def play(self, record):
        """Apply one match; return its list of `Change`s (empty if skipped)."""
        from . import elo

        record.bind(self.player)
        side1, side2 = record.sides()
        if not record.winner_side or not side1 or not side2:
            return []
        change1, change2, k1, k2 = elo.projected_elo_changes(record)
        changes = []
        for players, delta, k in ((side1, change1, k1), (side2, change2, k2)):
            for p in players:
                old = p.elo_rating
                p.elo_rating += delta
                p.elo_peak = max(p.elo_peak, p.elo_rating)
                p.matches_for_elo += 1
                changes.append(Change(p.pk, old, p.elo_rating, delta, k))
        return changes

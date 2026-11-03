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

from . import elo, seasons

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

    GUARD: migration 0040 runs this live code against *historical* models, so
    every field read here and in `load_context` must exist at 0039 forever:
    Match pk, date_played, created_at, match_type, best_of, is_double,
    winner_side, club_id, is_confirmed, is_live; MatchParticipant match_id,
    player_id, side, pk; Player pk, club_id; Season pk, club_id, starts_on,
    ends_on; PlayerRating/RatingChange as created by 0039. Renaming or
    removing any of them needs 0040 to be frozen to its own copy first.
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


# ---------------------------------------------------------------------------
# KAN-47: format scopes (PlayerRating) on the same traversal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeasonRecord:
    pk: int
    club_id: int
    starts_on: object
    ends_on: object


def load_context(*, season_model=None, player_model=None):
    """(seasons by club, player club ids): everything a replay needs besides matches."""
    if season_model is None or player_model is None:
        from .models import Player, Season

        season_model = season_model or Season
        player_model = player_model or Player
    seasons_by_club = {}
    for pk, club_id, starts_on, ends_on in season_model._base_manager.values_list(
        "pk", "club_id", "starts_on", "ends_on"
    ):
        seasons_by_club.setdefault(club_id, []).append(
            SeasonRecord(pk, club_id, starts_on, ends_on)
        )
    player_clubs = dict(player_model._base_manager.values_list("pk", "club_id"))
    return seasons_by_club, player_clubs


class MemoryBook(elo.RatingBook):
    """`elo.RatingState` rows, entirely in memory."""

    def __init__(self, player_clubs):
        super().__init__()
        self.player_clubs = player_clubs

    def new_row(self, player, scope, initial_rating):
        club_id = (
            scope.season.club_id if scope.season is not None
            else self.player_clubs.get(player.pk)
        )
        row = elo.RatingState(player.pk, club_id, scope.season_id, scope.format, initial_rating)
        self.rows[row.key] = row
        return row


class Replay:
    """Every scope `update_player_elo` writes, replayed in memory.

    `legacy` is the `LegacyReplay` (Player fields); `book` holds the format
    rows. `play()` mirrors `elo._apply_match`: same guards, same season
    resolution, the same `apply_format_scopes` step.
    """

    def __init__(self, seasons_by_club=None, player_clubs=None):
        self.legacy = LegacyReplay()
        self.seasons_by_club = seasons_by_club or {}
        self.book = MemoryBook(player_clubs or {})

    def play(self, record):
        """Apply one match: (legacy `Change`s, `elo.FormatChange`s)."""
        legacy_changes = self.legacy.play(record)  # also binds the stand-ins
        if not legacy_changes:
            return [], []
        season = seasons.resolve_season(
            record.date_played, self.seasons_by_club.get(record.club_id, ())
        )
        return legacy_changes, elo.apply_format_scopes(record, self.book, season)


def scope_filter(*, season_id=None, fmt=None):
    """A predicate on a rating row (state or model) for a scoped rebuild."""

    def keep(row):
        if season_id is not None and row.season_id != season_id:
            return False
        if fmt is not None and row.format != fmt:
            return False
        return True

    return keep


class RatingWriter:
    """Streams replayed format changes into PlayerRating/RatingChange in batches.

    Rows are created the first time a batch needs them (one bulk INSERT per
    batch), audit rows follow in the same batch, and the final values are
    written once at `finish()`. Model classes are parameters so a data
    migration can pass its historical ones.
    """

    def __init__(self, rating_model, change_model, *, keep=None,
                 batch_size=DEFAULT_CHUNK_SIZE):
        self.rating_model, self.change_model = rating_model, change_model
        self.keep = keep or (lambda row: True)
        self.batch_size = batch_size
        self.pks = {}  # state key -> PlayerRating pk
        self.states = {}
        self.buffer = []
        self.changes_written = 0

    def add(self, match_id, format_changes):
        for change in format_changes:
            if self.keep(change.row):
                self.buffer.append((match_id, change))
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        new = {}
        for _, change in self.buffer:
            state = change.row
            if state.key not in self.pks and state.key not in new:
                new[state.key] = state
        if new:
            created = self.rating_model._base_manager.bulk_create([
                self.rating_model(
                    player_id=s.player_id, club_id=s.club_id, season_id=s.season_id,
                    format=s.format, rating=s.rating, peak=s.peak, matches=s.matches,
                )
                for s in new.values()
            ])
            for key, obj in zip(new, created):
                self.pks[key] = obj.pk
                self.states[key] = new[key]
        self.change_model._base_manager.bulk_create([
            self.change_model(
                match_id=match_id, player_id=c.player_id, rating_id=self.pks[c.row.key],
                old_rating=c.old, new_rating=c.new, rating_change=c.change, k_factor=c.k,
            )
            for match_id, c in self.buffer
        ])
        self.changes_written += len(self.buffer)
        self.buffer = []

    def finish(self):
        """Flush and write every touched row's final rating, peak and count."""
        from django.utils import timezone

        self.flush()
        now = timezone.now()
        self.rating_model._base_manager.bulk_update(
            [
                self.rating_model(
                    pk=self.pks[key], rating=s.rating, peak=s.peak,
                    matches=s.matches, updated_at=now,
                )
                for key, s in self.states.items()
            ],
            ["rating", "peak", "matches", "updated_at"],
            batch_size=self.batch_size,
        )
        return len(self.states), self.changes_written


def default_models():
    from .models import Match, MatchParticipant, Player, PlayerRating, RatingChange, Season

    return dict(match=Match, participant=MatchParticipant, season=Season,
                player=Player, rating=PlayerRating, change=RatingChange)


class RebuildResult(NamedTuple):
    replay: Replay
    rows_written: int
    changes_written: int


def replay_history(*, models=None, keep=None, write_ratings=False,
                   chunk_size=DEFAULT_CHUNK_SIZE, on_match=None):
    """Run the full history through a `Replay`; optionally write PlayerRating rows.

    `on_match(index, record, legacy_changes)` is called after each match, which
    is how `recalculate_elo` writes EloHistory and progress from this one
    traversal instead of a second one. Returns a `RebuildResult`.
    """
    models = models or default_models()
    seasons_by_club, player_clubs = load_context(
        season_model=models["season"], player_model=models["player"]
    )
    replay = Replay(seasons_by_club, player_clubs)
    writer = (
        RatingWriter(models["rating"], models["change"], keep=keep, batch_size=chunk_size)
        if write_ratings else None
    )
    for index, record in enumerate(iter_replay_matches(
        chunk_size=chunk_size, match_model=models["match"],
        participant_model=models["participant"],
    ), 1):
        legacy_changes, format_changes = replay.play(record)
        if writer is not None:
            writer.add(record.pk, format_changes)
        if on_match is not None:
            on_match(index, record, legacy_changes)
    rows, changes = writer.finish() if writer is not None else (0, 0)
    return RebuildResult(replay, rows, changes)


def delete_player_ratings(*, models=None, keep=None, chunk_size=DEFAULT_CHUNK_SIZE):
    """Delete the PlayerRating rows `keep` selects (all by default) and their changes."""
    models = models or default_models()
    keep = keep or (lambda row: True)
    rows = models["rating"]._base_manager.values_list("pk", "season_id", "format")
    doomed = [pk for pk, season_id, fmt in rows if keep(_RowView(season_id, fmt))]
    for start in range(0, len(doomed), chunk_size):
        batch = doomed[start:start + chunk_size]
        models["change"]._base_manager.filter(rating_id__in=batch).delete()
        models["rating"]._base_manager.filter(pk__in=batch).delete()
    return len(doomed)


def rebuild_player_ratings(*, models=None, keep=None, chunk_size=DEFAULT_CHUNK_SIZE,
                           on_match=None):
    """Delete and rebuild PlayerRating/RatingChange (all, or the rows `keep` selects).

    Replays the full history either way: a season row's seed depends on the
    all-time rating before it, so even a scoped rebuild must compute
    everything and only *write* its scope. Callers own the transaction.

    `models` is a dict of model classes (see `default_models`) so a data
    migration can pass its historical ones.
    """
    models = models or default_models()
    delete_player_ratings(models=models, keep=keep, chunk_size=chunk_size)
    return replay_history(models=models, keep=keep, write_ratings=True,
                          chunk_size=chunk_size, on_match=on_match)


@dataclass(frozen=True)
class _RowView:
    season_id: object
    format: str

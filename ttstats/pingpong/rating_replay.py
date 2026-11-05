"""One deterministic traversal of the confirmed match history.

Glicko-2 with idle-time RD growth is path-dependent, so anything that
recomputes ratings from scratch must walk the history in exactly the same
order or two honest runs legitimately disagree. This module is that walk,
written once; `recalculate_ratings` reads it.

* **Scope:** matches with a winner **and** `is_confirmed=True`, not live.
  Championship matches can have a winner while unconfirmed; they are out, which
  is how `rating_pipeline.apply_match_ratings` behaves.
* **Order:** `REPLAY_ORDER` -- `date_played`, then `created_at`, then `pk` so a
  tie on both timestamps still has exactly one answer.
* **Memory:** matches stream through `.iterator(chunk_size=...)` and their
  participants are fetched one chunk at a time. Nothing materialises the
  whole history (the VPS has 3.7 GB).

Records are light stand-ins that quack like a `Match` as far as
`rating_pipeline.apply_format_scopes` cares (`winner_side`, `date_played`,
`is_double`, `sides()`), so the replay runs the *real* pipeline step rather
than a copy of it.
"""
from dataclasses import dataclass, field
from typing import NamedTuple

from django.db.models import OuterRef, Subquery

from . import glicko_adapter, rating_pipeline, seasons

REPLAY_ORDER = ("date_played", "created_at", "pk")
DEFAULT_CHUNK_SIZE = 500


@dataclass(frozen=True)
class ReplayPlayer:
    """A player as the pipeline sees one: a pk."""

    pk: int


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


class MemoryBook(rating_pipeline.RatingBook):
    """`rating_pipeline.RatingState` rows, entirely in memory."""

    def __init__(self, player_clubs):
        super().__init__()
        self.player_clubs = player_clubs

    def new_row(self, player, scope):
        club_id = (
            scope.season.club_id if scope.season is not None
            else self.player_clubs.get(player.pk)
        )
        row = rating_pipeline.RatingState(player.pk, club_id, scope.season_id, scope.format)
        self.rows[row.key] = row
        return row


class Replay:
    """Every scope `apply_match_ratings` writes, replayed in memory.

    `book` holds the format rows. `play()` mirrors
    `rating_pipeline._apply_match`: same guards (an empty side rates nothing),
    same season resolution, the same `apply_format_scopes` step.
    """

    def __init__(self, seasons_by_club=None, player_clubs=None, *,
                 params=glicko_adapter.DEFAULT_PARAMS):
        self.seasons_by_club = seasons_by_club or {}
        self.book = MemoryBook(player_clubs or {})
        self.params = params
        self._players = {}

    def _player(self, pk):
        if pk not in self._players:
            self._players[pk] = ReplayPlayer(pk)
        return self._players[pk]

    def play(self, record):
        """Apply one match; return its `rating_pipeline.FormatChange`s (empty if skipped)."""
        record.bind(self._player)
        side1, side2 = record.sides()
        if not record.winner_side or not side1 or not side2:
            return []
        season = seasons.resolve_season(
            record.date_played, self.seasons_by_club.get(record.club_id, ())
        )
        return rating_pipeline.apply_format_scopes(
            record, self.book, season, params=self.params
        )


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
    written once at `finish()`.
    """

    def __init__(self, rating_model, change_model, *, keep=None,
                 batch_size=DEFAULT_CHUNK_SIZE, frozen_elo=None):
        self.rating_model, self.change_model = rating_model, change_model
        # (match_id, player_id, season_id, format) -> the frozen Elo columns of
        # the row being rebuilt, so a rebuild never loses the Elo history.
        self.frozen_elo = frozen_elo or {}
        self.shadow_fields = list(glicko_adapter.SHADOW_FIELDS)
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
                    format=s.format, matches=s.matches,
                    **{f: getattr(s, f) for f in self.shadow_fields},
                )
                for s in new.values()
            ])
            for key, obj in zip(new, created):
                self.pks[key] = obj.pk
                self.states[key] = new[key]
        written = self.change_model._base_manager.bulk_create([
            self.change_model(
                match_id=match_id, player_id=c.player_id, rating_id=self.pks[c.row.key],
                **glicko_adapter.history_values(c.glicko),
                **self.frozen_elo.get(
                    (match_id, c.player_id, c.row.season_id, c.row.format), {}
                ),
            )
            for match_id, c in self.buffer
        ])
        self._stamp_match_dates([row.pk for row in written])
        self.changes_written += len(self.buffer)
        self.buffer = []

    def _stamp_match_dates(self, pks):
        """Date rebuilt audit rows by their match, not by the rebuild.

        `created_at` is `auto_now_add`, so bulk_create stamps "now"; the weekly
        movement and the rating chart read it, and a rebuild would otherwise
        make the whole history look like this week. One UPDATE per batch.
        """
        if not pks:
            return
        match_model = self.change_model._meta.get_field("match").related_model
        self.change_model._base_manager.filter(pk__in=pks).update(
            created_at=Subquery(
                match_model._base_manager.filter(pk=OuterRef("match_id")).values("date_played")[:1]
            )
        )

    def finish(self):
        """Flush and write every touched row's final Glicko state and count."""
        from django.utils import timezone

        self.flush()
        now = timezone.now()
        self.rating_model._base_manager.bulk_update(
            [
                self.rating_model(
                    pk=self.pks[key], matches=s.matches, updated_at=now,
                    **{f: getattr(s, f) for f in self.shadow_fields},
                )
                for key, s in self.states.items()
            ],
            ["matches", "updated_at", *self.shadow_fields],
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
                   chunk_size=DEFAULT_CHUNK_SIZE, on_match=None,
                   params=glicko_adapter.DEFAULT_PARAMS, frozen_elo=None):
    """Run the full history through a `Replay`; optionally write PlayerRating rows.

    `on_match(index, record, changes)` is called after each match (progress
    reporting). Returns a `RebuildResult`.
    """
    models = models or default_models()
    seasons_by_club, player_clubs = load_context(
        season_model=models["season"], player_model=models["player"]
    )
    replay = Replay(seasons_by_club, player_clubs, params=params)
    writer = (
        RatingWriter(models["rating"], models["change"], keep=keep, batch_size=chunk_size,
                     frozen_elo=frozen_elo)
        if write_ratings else None
    )
    for index, record in enumerate(iter_replay_matches(
        chunk_size=chunk_size, match_model=models["match"],
        participant_model=models["participant"],
    ), 1):
        changes = replay.play(record)
        if writer is not None:
            writer.add(record.pk, changes)
        if on_match is not None:
            on_match(index, record, changes)
    rows, changes = writer.finish() if writer is not None else (0, 0)
    return RebuildResult(replay, rows, changes)


ELO_HISTORY_FIELDS = ("elo_old_rating", "elo_new_rating", "elo_change", "elo_k_factor")


def frozen_elo_history(*, models=None, keep=None, chunk_size=DEFAULT_CHUNK_SIZE):
    """The frozen pre-KAN-73 Elo columns of the `RatingChange` rows `keep` selects.

    Keyed by (match_id, player_id, season_id, format), which survives a
    rebuild (the row pks do not). Only rows that have Elo values are kept, so
    the map is bounded by the pre-KAN-73 history and stops growing.
    """
    models = models or default_models()
    keep = keep or (lambda row: True)
    out = {}
    rows = models["change"]._base_manager.filter(elo_old_rating__isnull=False).values_list(
        "match_id", "player_id", "rating__season_id", "rating__format", *ELO_HISTORY_FIELDS
    ).order_by().iterator(chunk_size=chunk_size)
    for match_id, player_id, season_id, fmt, *values in rows:
        if keep(_RowView(season_id, fmt)):
            out[(match_id, player_id, season_id, fmt)] = dict(zip(ELO_HISTORY_FIELDS, values))
    return out


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
                           on_match=None, params=glicko_adapter.DEFAULT_PARAMS):
    """Delete and rebuild PlayerRating/RatingChange (all, or the rows `keep` selects).

    Replays the full history either way: a season row's seed depends on the
    all-time rating before it, so even a scoped rebuild must compute
    everything and only *write* its scope. Callers own the transaction.

    The frozen Elo columns of the rows it replaces are carried onto their
    rebuilt counterparts: no Elo history is lost or rewritten.
    """
    models = models or default_models()
    frozen = frozen_elo_history(models=models, keep=keep, chunk_size=chunk_size)
    delete_player_ratings(models=models, keep=keep, chunk_size=chunk_size)
    return replay_history(models=models, keep=keep, write_ratings=True,
                          chunk_size=chunk_size, on_match=on_match, params=params,
                          frozen_elo=frozen)


@dataclass(frozen=True)
class _RowView:
    season_id: object
    format: str

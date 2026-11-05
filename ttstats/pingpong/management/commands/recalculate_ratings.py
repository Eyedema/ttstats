"""
Rebuild every rating (PlayerRating + RatingChange) from the confirmed history.

Usage:
    python manage.py recalculate_ratings                      # full rebuild
    python manage.py recalculate_ratings --dry-run            # per-player diff, writes nothing
    python manage.py recalculate_ratings --format doubles     # rebuild only doubles rows
    python manage.py recalculate_ratings --season 7           # rebuild only season 7's rows

Replaces the Elo-era rebuild command (KAN-73). What it rebuilds:

* **A full run** deletes and rebuilds every `PlayerRating` and `RatingChange`
  row by replaying the history through Glicko-2. `EloHistory` is frozen and
  never touched, and the frozen `RatingChange.elo_*` columns are carried onto
  the rebuilt rows unchanged.
* **`--season` / `--format`** rebuild only the rows in that scope. The whole
  history is still replayed, because a season rating is seeded from the
  all-time rating before it.

Deterministic: the replay order is `date_played, created_at, pk`, through
`rating_replay`. Streamed with `.iterator(chunk_size=...)` and written in
batches, so it never holds the history in memory (the VPS has 3.7 GB).

**All or nothing:** everything happens in one transaction, so an
interruption leaves ratings fully old.

**Not safe to run while matches are being confirmed.** A real run takes
`select_for_update` on every Player row for its duration. A confirmation
arriving mid-rebuild waits for it rather than interleaving, because
`apply_match_ratings` locks its match's Player rows before reading any rating
(on PostgreSQL; SQLite locks the whole database anyway). It then applies on
top of the rebuilt values. At this scale that is seconds; run it when nobody
is playing.

`--dry-run` replays in memory and diffs stored against replayed state per
player and scope: `matches` exactly, the Glicko floats within 1e-6,
`last_rated_on` exactly. Diffs of players in a match confirmed out of date
order (live ratings apply in confirmation order, the replay in play order) are
listed as explained; any unexplained diff is a bug.
"""

import math

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pingpong import cache_utils
from pingpong.glicko_adapter import SHADOW_FIELDS
from pingpong.models import (
    Match,
    Player,
    PlayerRating,
    RatingChange,
    RatingFormat,
    Season,
)
from pingpong.rating_replay import (
    DEFAULT_CHUNK_SIZE,
    confirmed_history,
    rebuild_player_ratings,
    replay_history,
    scope_filter,
)

PROGRESS_EVERY = 50
# Every stored field compared by the dry run: the match count, then the Glicko state.
FIELDS = ("matches", *SHADOW_FIELDS)
# The Glicko floats are compared to this absolute tolerance; the rest exactly.
GLICKO_TOLERANCE = 1e-6


class Command(BaseCommand):
    help = 'Rebuild Glicko-2 ratings (PlayerRating and RatingChange) from all confirmed matches'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Recompute in memory and print a per-player diff against stored values; write nothing',
        )
        parser.add_argument(
            '--season',
            type=int,
            default=None,
            help="Rebuild only this season's rows",
        )
        parser.add_argument(
            '--format',
            choices=[c.value for c in RatingFormat],
            default=None,
            help="Rebuild only this format's rows",
        )
        parser.add_argument(
            '--chunk-size',
            type=int,
            default=DEFAULT_CHUNK_SIZE,
            help='Matches per batch while streaming history',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        season_id, fmt = options['season'], options['format']
        chunk_size = options['chunk_size']

        if season_id is not None and not Season.all_objects.filter(pk=season_id).exists():
            raise CommandError(f'Season {season_id} does not exist')
        keep = scope_filter(season_id=season_id, fmt=fmt)

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be saved'))

        match_count = confirmed_history(Match._base_manager).count()
        self.stdout.write(f'Found {match_count} confirmed matches to process')
        if season_id is not None or fmt is not None:
            self.stdout.write(
                f'Scope: PlayerRating rows for season={season_id or "any"}, '
                f'format={fmt or "any"}'
            )

        if dry_run:
            self._dry_run(match_count, keep, chunk_size)
        else:
            self._rebuild(match_count, keep, chunk_size)

    # -- a real run -----------------------------------------------------------

    def _rebuild(self, match_count, keep, chunk_size):
        def on_match(index, record, changes):
            if index % PROGRESS_EVERY == 0:
                self.stdout.write(f'  Processed {index}/{match_count} matches...')

        with transaction.atomic():
            # Hold every Player row for the duration: a confirmation arriving
            # mid-rebuild waits instead of interleaving with the replay.
            list(Player._base_manager.select_for_update().values_list('pk', flat=True))

            self.stdout.write('Recalculating ratings...')
            result = rebuild_player_ratings(keep=keep, chunk_size=chunk_size, on_match=on_match)
            self.stdout.write(
                f'Rebuilt {result.rows_written} PlayerRating rows and '
                f'{result.changes_written} rating changes'
            )
            # Bulk writes fire no post_save, so nothing invalidated per player.
            transaction.on_commit(cache_utils.invalidate_all_caches)

        self.stdout.write(
            self.style.SUCCESS(f'✓ Successfully recalculated ratings for {match_count} matches')
        )

    # -- --dry-run ---------------------------------------------------------------

    def _dry_run(self, match_count, keep, chunk_size):
        self.stdout.write(f'Would recalculate ratings for {match_count} matches')
        replay = replay_history(chunk_size=chunk_size).replay
        self._print_diffs(replay, keep)

    # -- the diff (KAN-49, KAN-69) -----------------------------------------------

    def _diffs(self, replay, keep):
        """[(player pk, name, scope label, {field: (stored, recomputed)})].

        The live-versus-replay check. Floats compare to `GLICKO_TOLERANCE`,
        everything else exactly; a row on one side only is a diff.
        """
        names = dict(Player._base_manager.values_list('pk', 'name'))
        season_names = dict(Season.all_objects.values_list('pk', 'name'))
        stored = {}
        for row in PlayerRating.all_objects.values_list(
            'player_id', 'season_id', 'format', *FIELDS
        ).iterator(chunk_size=DEFAULT_CHUNK_SIZE):
            if keep(_Key(*row[:3])):
                stored[row[:3]] = row[3:]
        recomputed = {
            key: tuple(getattr(state, f) for f in FIELDS)
            for key, state in replay.book.rows.items() if keep(state)
        }

        out = []
        for key in sorted(set(stored) | set(recomputed), key=lambda k: (k[0], k[1] or 0, k[2])):
            a, b = stored.get(key), recomputed.get(key)
            if a is None or b is None:
                changed = {
                    f: (a[i] if a else 'missing', b[i] if b else 'missing')
                    for i, f in enumerate(FIELDS)
                }
            else:
                changed = {
                    f: (a[i], b[i]) for i, f in enumerate(FIELDS)
                    if not _same(a[i], b[i])
                }
            if not changed:
                continue
            player_id, season_id, fmt = key
            label = f'{fmt} {season_names.get(season_id, season_id) if season_id else "all-time"}'
            out.append((player_id, names.get(player_id, f'#{player_id}'), label, changed))
        return out

    def _out_of_order_matches(self):
        """(match ids, player ids) of matches applied after a later-played one.

        Live ratings are applied in confirmation order and a replay in play
        order, so a match confirmed out of date order legitimately moves the
        state away from the replay. Per rating row, walk its RatingChange in
        application order; a match whose replay key is below the running
        maximum was applied out of order. Streams with O(1) state per row.
        """
        late, running_max, current = set(), None, None
        rows = RatingChange._base_manager.order_by(
            'rating_id', 'created_at', 'pk'
        ).values_list(
            'rating_id', 'match_id', 'match__date_played', 'match__created_at'
        ).iterator(chunk_size=DEFAULT_CHUNK_SIZE)
        for rating_id, match_id, played, created in rows:
            if rating_id != current:
                current, running_max = rating_id, None
            key = (played, created, match_id)
            if running_max is not None and key < running_max:
                late.add(match_id)
            elif running_max is None or key > running_max:
                running_max = key
        players, ids = set(), sorted(late)
        for start in range(0, len(ids), 500):
            players.update(
                RatingChange._base_manager.filter(match_id__in=ids[start:start + 500])
                .values_list('player_id', flat=True)
            )
        return late, players

    def _print_diffs(self, replay, keep):
        diffs = self._diffs(replay, keep)
        late, late_players = self._out_of_order_matches() if diffs else (set(), set())
        explained = [d for d in diffs if d[0] in late_players]
        unexplained = [d for d in diffs if d[0] not in late_players]

        if diffs:
            self.stdout.write('\nPer-player differences (stored -> recomputed):')
            for tag, group in (('', unexplained), ('[out of date order] ', explained)):
                for _, name, label, changed in group:
                    cells = '; '.join(f'{f}: {s} -> {r}' for f, (s, r) in changed.items())
                    self.stdout.write(f'  {tag}{name[:24]:<24} {label[:28]:<28} {cells}')
        summary = (
            f'\nDry run: {len(diffs)} scope rows differ '
            f'({len(unexplained)} unexplained, {len(explained)} explained by '
            f'matches confirmed out of date order)'
        )
        if unexplained:
            self.stdout.write(self.style.ERROR(summary))
        elif explained:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
        if late:
            self.stdout.write(
                f'Matches confirmed out of date order ({len(late)}): '
                + ', '.join(f'match {pk}' for pk in sorted(late))
                + ". Their players' diffs are expected; a replay is the fix."
            )
            self.stdout.write(
                '(A player is "explained" when they played any such match; an '
                "opponent's later diffs inherit it, so check unexplained ones first.)"
            )


def _same(a, b):
    """Equal stored values: floats within `GLICKO_TOLERANCE`, anything else exactly."""
    if not isinstance(a, float) or not isinstance(b, float):
        return a == b
    return math.isclose(a, b, rel_tol=0, abs_tol=GLICKO_TOLERANCE)


class _Key:
    """A (season_id, format) view of a stored row key, for `scope_filter`."""

    __slots__ = ('format', 'season_id')

    def __init__(self, player_id, season_id, fmt):
        self.season_id, self.format = season_id, fmt

"""
Management command to recalculate Elo ratings for all existing matches.
Run after initial Elo system deployment or to fix rating inconsistencies.

Usage:
    python manage.py recalculate_elo                      # full rebuild
    python manage.py recalculate_elo --dry-run            # per-player diff, writes nothing
    python manage.py recalculate_elo --format doubles     # rebuild only doubles PlayerRating rows
    python manage.py recalculate_elo --season 7           # rebuild only season 7's rows

What it rebuilds (KAN-49):

* **A full, unscoped run** resets `Player.elo_rating`/`elo_peak`/
  `matches_for_elo`, rewrites `EloHistory` (a new generation: the old rows are
  deleted, as this command always did), and rebuilds every `PlayerRating` and
  `RatingChange` row.
* **`--season` / `--format`** rebuild only the PlayerRating rows in that
  scope. The legacy fields and `EloHistory` are left alone. The whole history
  is still replayed, because a season rating is seeded from the all-time
  rating before it.

Deterministic: the replay order is `date_played, created_at, pk`, through
`elo_replay` -- the same traversal the parity harness uses. Batched with
`.iterator(chunk_size=...)`, so it never holds the history in memory.

**All or nothing:** everything happens in one transaction, so an
interruption leaves ratings fully old.

**Not safe to run while matches are being confirmed.** A real run takes
`select_for_update` on every Player row for its duration, so a confirmation
arriving mid-rebuild waits for it rather than interleaving (on PostgreSQL;
SQLite locks the whole database anyway). At this scale that is seconds; run it
when nobody is playing.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pingpong import cache_utils
from pingpong.elo_replay import (
    DEFAULT_CHUNK_SIZE,
    confirmed_history,
    rebuild_player_ratings,
    replay_history,
    scope_filter,
)
from pingpong.models import EloHistory, Match, Player, PlayerRating, RatingFormat, Season

PROGRESS_EVERY = 50
FIELDS = ("rating", "peak", "matches")


class Command(BaseCommand):
    help = 'Recalculate Elo ratings (legacy fields and PlayerRating) for all confirmed matches'

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
            help='Rebuild only this season\'s PlayerRating rows (legacy Elo untouched)',
        )
        parser.add_argument(
            '--format',
            choices=[c.value for c in RatingFormat],
            default=None,
            help='Rebuild only this format\'s PlayerRating rows (legacy Elo untouched)',
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
        scoped = season_id is not None or fmt is not None

        if season_id is not None and not Season.all_objects.filter(pk=season_id).exists():
            raise CommandError(f'Season {season_id} does not exist')
        keep = scope_filter(season_id=season_id, fmt=fmt)

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be saved'))

        match_count = confirmed_history(Match._base_manager).count()
        self.stdout.write(f'Found {match_count} confirmed matches to process')
        if scoped:
            self.stdout.write(
                f'Scope: PlayerRating rows for season={season_id or "any"}, '
                f'format={fmt or "any"}; legacy Elo and EloHistory untouched'
            )

        if dry_run:
            self._dry_run(match_count, keep, scoped, chunk_size)
        else:
            self._rebuild(match_count, keep, scoped, chunk_size)

    # -- a real run -----------------------------------------------------------

    def _rebuild(self, match_count, keep, scoped, chunk_size):
        history_buffer = []

        def flush_history():
            EloHistory.objects.bulk_create(history_buffer)
            history_buffer.clear()

        def on_match(index, record, legacy_changes):
            if not scoped:
                history_buffer.extend(
                    EloHistory(
                        match_id=record.pk, player_id=c.player_id, old_rating=c.old,
                        new_rating=c.new, rating_change=c.change, k_factor=c.k,
                    )
                    for c in legacy_changes
                )
                if len(history_buffer) >= chunk_size:
                    flush_history()
            if index % PROGRESS_EVERY == 0:
                self.stdout.write(f'  Processed {index}/{match_count} matches...')

        with transaction.atomic():
            # Hold every Player row for the duration: a confirmation arriving
            # mid-rebuild waits instead of interleaving with the replay.
            list(Player._base_manager.select_for_update().values_list('pk', flat=True))

            if not scoped:
                # Reset all player Elo ratings
                self.stdout.write('Resetting all player Elo ratings to 1500...')
                Player._base_manager.update(elo_rating=1500, elo_peak=1500, matches_for_elo=0)

                # Clear existing Elo history
                deleted_count = EloHistory.objects.count()
                EloHistory.objects.all().delete()
                self.stdout.write(f'Deleted {deleted_count} existing Elo history records')

            # Replay matches in chronological order
            self.stdout.write('Recalculating Elo ratings...')
            result = rebuild_player_ratings(keep=keep, chunk_size=chunk_size, on_match=on_match)

            if not scoped:
                flush_history()
                Player._base_manager.bulk_update(
                    [
                        Player(pk=p.pk, elo_rating=p.elo_rating, elo_peak=p.elo_peak,
                               matches_for_elo=p.matches_for_elo)
                        for p in result.replay.legacy.players.values()
                    ],
                    ['elo_rating', 'elo_peak', 'matches_for_elo'],
                    batch_size=chunk_size,
                )

            self.stdout.write(
                f'Rebuilt {result.rows_written} PlayerRating rows and '
                f'{result.changes_written} rating changes'
            )
            # Bulk writes fire no post_save, so nothing invalidated per player.
            transaction.on_commit(cache_utils.invalidate_all_caches)

        self.stdout.write(self.style.SUCCESS(f'✓ Successfully recalculated Elo for {match_count} matches'))

    # -- --dry-run ---------------------------------------------------------------

    def _dry_run(self, match_count, keep, scoped, chunk_size):
        if not scoped:
            self.stdout.write('Would reset all player Elo ratings to 1500')
            self.stdout.write(f'Would delete {EloHistory.objects.count()} existing history records')
        self.stdout.write(f'Would recalculate Elo for {match_count} matches')

        replay = replay_history(chunk_size=chunk_size).replay
        diffs = self._diffs(replay, keep, scoped)
        self._print_diffs(diffs)

    def _diffs(self, replay, keep, scoped):
        """[(player name, player pk, scope label, {field: (stored, recomputed)})]."""
        names = dict(Player._base_manager.values_list('pk', 'name'))
        season_names = dict(Season.all_objects.values_list('pk', 'name'))
        out = []

        if not scoped:
            for pk, rating, peak, matches in Player._base_manager.values_list(
                'pk', 'elo_rating', 'elo_peak', 'matches_for_elo'
            ).order_by('pk'):
                p = replay.legacy.players.get(pk)
                recomputed = (p.elo_rating, p.elo_peak, p.matches_for_elo) if p else (1500, 1500, 0)
                changed = {
                    f: (s, r) for f, s, r in zip(FIELDS, (rating, peak, matches), recomputed) if s != r
                }
                if changed:
                    out.append((names.get(pk, f'#{pk}'), pk, 'legacy', changed))

        stored = {}
        for row in PlayerRating.all_objects.values_list(
            'player_id', 'season_id', 'format', 'rating', 'peak', 'matches'
        ).iterator(chunk_size=DEFAULT_CHUNK_SIZE):
            stored[row[:3]] = row[3:]
        recomputed = {
            key: (s.rating, s.peak, s.matches)
            for key, s in replay.book.rows.items() if keep(s)
        }
        stored = {k: v for k, v in stored.items() if keep(_Key(*k))}

        for key in sorted(set(stored) | set(recomputed), key=lambda k: (k[0], k[1] or 0, k[2])):
            player_id, season_id, fmt = key
            a, b = stored.get(key), recomputed.get(key)
            if a == b:
                continue
            if a is None or b is None:
                changed = {f: (a[i] if a else 'missing', b[i] if b else 'missing')
                           for i, f in enumerate(FIELDS)}
            else:
                changed = {f: (a[i], b[i]) for i, f in enumerate(FIELDS) if a[i] != b[i]}
            label = f'{fmt} {season_names.get(season_id, season_id) if season_id else "all-time"}'
            out.append((names.get(player_id, f'#{player_id}'), player_id, label, changed))
        out.sort(key=lambda d: (d[0], d[1], d[2]))
        return out

    def _print_diffs(self, diffs):
        players = {pk for _, pk, _, _ in diffs}
        if diffs:
            self.stdout.write('\nPer-player differences (stored -> recomputed):')
            self.stdout.write(
                f'  {"player":<24} {"scope":<28} {"rating":<22} {"peak":<22} {"matches":<18}'
            )
            for name, _, label, changed in diffs:
                cells = [
                    f'{changed[f][0]} -> {changed[f][1]}' if f in changed else '='
                    for f in FIELDS
                ]
                self.stdout.write(
                    f'  {name[:24]:<24} {label[:28]:<28} {cells[0]:<22} {cells[1]:<22} {cells[2]:<18}'
                )
        summary = (
            f'\nDry run: {len(players)} players differ '
            f'({len(diffs)} scope rows) between stored and recomputed ratings'
        )
        if diffs:
            self.stdout.write(self.style.WARNING(summary))
            self.stdout.write(
                'Stored values can differ from a replay because live Elo is applied '
                'in confirmation order and the replay uses play order. A real run '
                'rewrites them.'
            )
        else:
            self.stdout.write(self.style.SUCCESS(summary))


class _Key:
    """A (season_id, format) view of a stored row key, for `scope_filter`."""

    __slots__ = ('season_id', 'format')

    def __init__(self, player_id, season_id, fmt):
        self.season_id, self.format = season_id, fmt

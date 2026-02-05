"""Management command for cache operations."""
from django.core.management.base import BaseCommand
from django.core.cache import cache

from pingpong.cache_utils import invalidate_all_caches, get_cache_stats


class Command(BaseCommand):
    help = 'Manage Redis cache for TTStats'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear all caches',
        )
        parser.add_argument(
            '--stats',
            action='store_true',
            help='Show cache statistics',
        )
        parser.add_argument(
            '--test',
            action='store_true',
            help='Test cache connectivity',
        )

    def handle(self, *args, **options):
        if options['clear']:
            self.stdout.write('Clearing all caches...')
            invalidate_all_caches()
            self.stdout.write(self.style.SUCCESS('All caches cleared'))

        elif options['stats']:
            self.stdout.write('Cache Statistics:')
            stats = get_cache_stats()
            if 'error' in stats:
                self.stdout.write(self.style.WARNING(f'  {stats["error"]}'))
            else:
                self.stdout.write(f'  Total keys: {stats["keys"]}')
                self.stdout.write(f'  Memory used: {stats["memory"]}')
                self.stdout.write(f'  Cache hits: {stats["hits"]}')
                self.stdout.write(f'  Cache misses: {stats["misses"]}')
                self.stdout.write(f'  Hit rate: {stats["hit_rate"]:.2f}%')

        elif options['test']:
            self.stdout.write('Testing cache connectivity...')
            try:
                cache.set('test_key', 'test_value', 30)
                value = cache.get('test_key')
                cache.delete('test_key')

                if value == 'test_value':
                    self.stdout.write(self.style.SUCCESS('Cache is working!'))
                else:
                    self.stdout.write(self.style.ERROR('Cache test failed: value mismatch'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Cache error: {e}'))

        else:
            self.stdout.write(self.style.WARNING('Use --clear, --stats, or --test'))

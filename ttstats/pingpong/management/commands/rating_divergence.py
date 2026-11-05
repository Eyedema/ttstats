"""Compare Elo and Glicko-2 over the whole confirmed history (KAN-66).

Usage:
    python manage.py rating_divergence
    python manage.py rating_divergence --tau 0.3 --period-days 14 --top 20
    python manage.py rating_divergence --json

Read-only: it replays history in memory with no user in context (so it sees
every club) and writes nothing. See `pingpong/rating_divergence.py`.
"""

import json

from django.core.management.base import BaseCommand

from pingpong.elo_replay import DEFAULT_CHUNK_SIZE
from pingpong.glicko_adapter import DEFAULT_PARAMS
from pingpong.rating_divergence import DEFAULT_TINY_RD, DEFAULT_TOP, run_divergence


class Command(BaseCommand):
    help = "Report how Glicko-2 predictions and rankings differ from Elo over real history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tau",
            type=float,
            default=None,
            help=f"Glicko-2 tau (default {DEFAULT_PARAMS.tau}).",
        )
        parser.add_argument(
            "--period-days",
            type=float,
            default=None,
            help=f"Days per idle rating period (default {DEFAULT_PARAMS.period_days}).",
        )
        parser.add_argument(
            "--season-rd-floor",
            type=float,
            default=None,
            help=f"Minimum RD of a seeded season row (default {DEFAULT_PARAMS.season_rd_floor}).",
        )
        parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
        parser.add_argument(
            "--top",
            type=int,
            default=DEFAULT_TOP,
            help="How many biggest rank movers to list.",
        )
        parser.add_argument(
            "--tiny-rd",
            type=float,
            default=DEFAULT_TINY_RD,
            help="End RD at or above which a player is 'not comparable'.",
        )
        parser.add_argument(
            "--json", action="store_true", help="Print the report as JSON."
        )

    def handle(self, *args, **options):
        overrides = {
            name: options[option]
            for name, option in (
                ("tau", "tau"),
                ("period_days", "period_days"),
                ("season_rd_floor", "season_rd_floor"),
            )
            if options[option] is not None
        }
        params = DEFAULT_PARAMS._replace(**overrides)
        report = run_divergence(
            params=params,
            chunk_size=options["chunk_size"],
            tiny_rd=options["tiny_rd"],
            top=options["top"],
        )
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            self.stdout.write(report.render())

"""Run the Elo parity harness (KAN-45) against whatever database is configured.

Usage:
    python manage.py elo_parity                       # the gate: exits non-zero on any diff
    python manage.py elo_parity --inject-k-factor 40  # prove it fails on a real divergence
    python manage.py elo_parity --compare-stored      # also compare stored EloHistory

Read-only: it replays history in memory and writes nothing, so it is safe to
point at a production snapshot. See `pingpong/elo_parity.py`.
"""
from django.core.management.base import BaseCommand, CommandError

from pingpong.elo_parity import run_parity
from pingpong.elo_replay import DEFAULT_CHUNK_SIZE


class Command(BaseCommand):
    help = "Replay Elo through the frozen reference and the live pipeline; fail on any diff."

    def add_arguments(self, parser):
        parser.add_argument(
            "--inject-k-factor",
            type=float,
            default=None,
            help="Replace the pipeline's base K for this run (the harness must then fail).",
        )
        parser.add_argument(
            "--compare-stored",
            action="store_true",
            help="Also compare stored EloHistory rows with the reference replay.",
        )
        parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)

    def handle(self, *args, **options):
        report = run_parity(
            inject_k_factor=options["inject_k_factor"],
            compare_stored_history=options["compare_stored"],
            chunk_size=options["chunk_size"],
        )
        text = report.render()
        if report.ok:
            self.stdout.write(self.style.SUCCESS(text))
            return
        self.stdout.write(text)
        raise CommandError("Elo parity failed: see the diff table above.")

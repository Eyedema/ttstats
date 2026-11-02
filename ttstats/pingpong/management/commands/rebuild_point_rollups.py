"""Rebuild MatchPointSummary rows from the raw point log (KAN-30 / S2.4).

The rollups are a cache. This command is what makes that claim true: every row
is reproducible from `PointEvent`, so a failed write at match completion, a
changed statistic, or a repaired log costs a rebuild rather than lost data.

Runs unscoped, like every other management command: there is no viewer, so the
managers' "no user means unfiltered" contract applies and `all_objects` is used
deliberately -- a rebuild that skipped every club but one would leave exactly
the silent, partial state the rollups exist to avoid.
"""
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from pingpong import point_rollups
from pingpong.models import Match, MatchPointSummary, PointEvent


class Command(BaseCommand):
    help = "Rebuild (or verify) point-level rollups from the raw point log."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Compare rollups against the raw log without writing anything.",
        )
        parser.add_argument(
            "--match",
            type=int,
            default=None,
            help="Restrict to one match id.",
        )

    def handle(self, *args, **options):
        # all_objects: a command has no viewer, and a rollup rebuild that saw
        # only one club's matches would be worse than none.
        matches = Match.all_objects.filter(
            Exists(PointEvent.objects.filter(match=OuterRef("pk")))
        ).order_by("pk")
        if options["match"] is not None:
            matches = matches.filter(pk=options["match"])

        if options["check"]:
            return self._check(matches)
        return self._rebuild(matches)

    def _rebuild(self, matches):
        rebuilt = rows = 0
        for match in matches:
            rows += point_rollups.rebuild_match_summaries(match)
            rebuilt += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt {rows} summary rows across {rebuilt} matches."
            )
        )

    def _check(self, matches):
        """Report drift; never repair. A check that writes is not a check."""
        checked = 0
        drift = []
        for match in matches:
            checked += 1
            problem = point_rollups.check_match(match)
            if problem is not None:
                drift.append(problem)

        # A match with rollups but no log is invisible to the queryset above,
        # so look for it from the other direction.
        orphans = list(
            MatchPointSummary.objects.exclude(
                Exists(PointEvent.objects.filter(match=OuterRef("match_id")))
            ).values_list("match_id", flat=True).distinct()
        )

        self.stdout.write(f"Checked {checked} matches with a point log.")
        if orphans:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(orphans)} matches have rollups but no point log: {orphans}"
                )
            )
        for problem in drift:
            self.stdout.write(self.style.ERROR(f"Drift: {problem}"))
        if not drift and not orphans:
            self.stdout.write(self.style.SUCCESS("Rollups agree with the raw log."))

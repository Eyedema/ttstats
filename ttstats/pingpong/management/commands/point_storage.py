"""Row-count and growth reporting for the point log (KAN-35 / T2.8).

**The retention policy is: keep every `PointEvent` row, indefinitely.** The
S2.4 rollups mean no page ever scans raw point rows, so nothing gets slower as
the table grows -- only the disk fills, and this epic's own sizing says that
takes a very long time. Deleting data to solve a problem the app does not have
would be the more expensive choice, and it is irreversible.

That decision has a **trigger point rather than an expiry date**, which is what
makes it a policy and not an omission. This command is how the trigger is
observed: `REVISIT_ROWS` total rows, or `REVISIT_ROWS_PER_YEAR` of growth,
and the report says so in red rather than letting the number slide past.

Modelled on `cache_control --stats`: an operational introspection command with
flags and a plain summary. Deliberately a *separate* command -- `cache_control`
answers questions about Redis, and folding a Postgres table report into it
would make one command answer two unrelated operational questions.

**What is lost if raw rows are ever pruned**, and what is not: the rollups
carry every *career* and *head-to-head* statistic the app shows, so player
detail and the head-to-head comparison would be unaffected. Only match
detail's momentum chart and run timeline read the raw log, so a pruned match
would lose its chart -- and, correctly, render the section as absent rather
than empty. `test_point_retention.py` pins that. Any pruning job also needs
Epic 3's T3.6 scheduled-job runner, which does not exist; until then this
ticket is the policy plus this report.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count, Min
from django.utils import timezone

from pingpong.models import Match, MatchPointSummary, PointEvent

# The trigger point, not a limit. ~150 bytes per row once the two indexes are
# counted, so a million rows is roughly 150-200MB -- material on a 3.7GB VPS,
# and far enough away that the policy holds for years at one club's usage
# (~60-100 rows per match, ~50,000 rows per 500-match year).
REVISIT_ROWS = 1_000_000

# Or growth that would reach it inside a handful of years. Either alone is
# enough to revisit; the table is meant to grow with club count, not just with
# one group's play.
REVISIT_ROWS_PER_YEAR = 250_000

WINDOW_DAYS = 30


class Command(BaseCommand):
    help = "Report point-log row counts, growth rate and the revisit threshold."

    def add_arguments(self, parser):
        parser.add_argument(
            "--by-club",
            action="store_true",
            help="Break the row counts down by club.",
        )

    def handle(self, *args, **options):
        # Unscoped on purpose: a management command has no viewer, and a
        # storage report that saw one club's rows would be worse than none.
        events = PointEvent.objects.all()
        total = events.count()
        summaries = MatchPointSummary.objects.count()
        matches = (
            Match.all_objects.filter(point_events__isnull=False).distinct().count()
        )

        self.stdout.write("Point log storage:")
        self.stdout.write(f"  PointEvent rows:        {total}")
        self.stdout.write(f"  MatchPointSummary rows: {summaries}")
        self.stdout.write(f"  Matches with a log:     {matches}")
        if matches:
            self.stdout.write(f"  Points per match (avg): {total / matches:.1f}")

        rate = self._growth(events, total)

        if options["by_club"]:
            self._by_club(events)

        self._threshold(total, rate)

    def _growth(self, events, total):
        """Rows added in the last WINDOW_DAYS, annualised.

        Falls back to the whole history when the table is younger than the
        window -- a 30-day rate extrapolated from three days of data is a
        number that would trigger the threshold on the first busy week.
        """
        now = timezone.now()
        since = now - timezone.timedelta(days=WINDOW_DAYS)
        recent = events.filter(created_at__gte=since).count()

        oldest = events.aggregate(first=Min("created_at"))["first"]
        if oldest is None:
            self.stdout.write("  Growth:                 no rows yet")
            return 0.0

        span_days = max((now - oldest).days, 1)
        if span_days < WINDOW_DAYS:
            per_day = total / span_days
            basis = f"whole history, {span_days}d"
        else:
            per_day = recent / WINDOW_DAYS
            basis = f"last {WINDOW_DAYS}d"

        per_year = per_day * 365
        self.stdout.write(
            f"  Growth:                 {per_day:.0f} rows/day "
            f"= {per_year:.0f} rows/year ({basis})"
        )
        return per_year

    def _by_club(self, events):
        rows = (
            events.values("match__club_id", "match__club__name")
            .annotate(rows=Count("id"))
            .order_by("-rows")
        )
        self.stdout.write("  By club:")
        for row in rows:
            name = row["match__club__name"] or "(no club)"
            self.stdout.write(f"    {name}: {row['rows']}")

    def _threshold(self, total, per_year):
        """Surfaced, never silently passed."""
        self.stdout.write(
            f"  Revisit at:             {REVISIT_ROWS} rows "
            f"or {REVISIT_ROWS_PER_YEAR} rows/year"
        )

        crossed = []
        if total >= REVISIT_ROWS:
            crossed.append(f"{total} rows is at or past {REVISIT_ROWS}")
        if per_year >= REVISIT_ROWS_PER_YEAR:
            crossed.append(
                f"{per_year:.0f} rows/year is at or past {REVISIT_ROWS_PER_YEAR}"
            )

        if crossed:
            self.stdout.write(
                self.style.ERROR(
                    "  THRESHOLD CROSSED -- revisit the retention policy: "
                    + "; ".join(crossed)
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("  Within the retention threshold.")
            )

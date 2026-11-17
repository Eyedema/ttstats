"""Run the scheduled jobs (KAN-37 / T3.6). Host cron calls this once a day.

See ``deploy/cron/`` for the crontab and ``pingpong/jobs.py`` for the runner.
A failing job is reported on stderr and recorded as a FAILED JobRun, but the
command still runs the remaining jobs and exits normally: one broken job must
not stop the others.
"""
from django.core.management.base import BaseCommand, CommandError

from pingpong import jobs
from pingpong.models import JobRun
from ttstats import sentry


class Command(BaseCommand):
    help = "Run every registered scheduled job (or one, with --job)."

    def add_arguments(self, parser):
        parser.add_argument("--job", default=None, help="Run only this job.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Pass dry_run to every job: report intended actions, write nothing.",
        )
        parser.add_argument(
            "--list", action="store_true", help="List the registered jobs and exit."
        )

    def handle(self, *args, **options):
        names = jobs.job_names()

        if options["list"]:
            for name in names:
                self.stdout.write(name)
            return

        if options["job"]:
            if options["job"] not in names:
                raise CommandError(
                    f"Unknown job {options['job']!r}. Registered: {', '.join(names)}"
                )
            names = [options["job"]]

        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: nothing will be written"))

        # Only the nightly run checks in (KAN-111): a hand `--job X` or a dry
        # run is not the schedule, and would mark a missed night as fine.
        # Any job not ending SUCCESS makes the check-in an error: a FAILED
        # job, or a SKIPPED one (cache unreachable, or an overlapping run).
        monitored = not options["job"] and not dry_run
        with sentry.cron_monitor(
            sentry.SCHEDULED_JOBS_MONITOR,
            sentry.SCHEDULED_JOBS_MONITOR_CONFIG,
            enabled=monitored,
        ) as checkin:
            results = [self._run(name, dry_run) for name in names]
            checkin.ok = all(r.status == JobRun.Status.SUCCESS for r in results)

    def _run(self, name, dry_run):
        result = jobs.run_job(name, dry_run=dry_run)
        line = f"{name}: {result.status}"
        if result.summary:
            line += f" - {result.summary}"
        if result.status == JobRun.Status.FAILED:
            last = result.error.strip().splitlines()[-1:] or ['']
            self.stderr.write(self.style.ERROR(f"{line} - {last[0]}"))
        elif result.status == JobRun.Status.SKIPPED:
            self.stdout.write(self.style.WARNING(line))
        else:
            self.stdout.write(self.style.SUCCESS(line))
        return result

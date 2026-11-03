"""The jobs host cron runs nightly through ``run_scheduled_jobs``.

One decorated function per job; ``jobs.py`` owns locking, logging and failure
isolation. Every job takes ``dry_run``, is idempotent, works in batches, and
returns a one-line summary. Jobs run in the order they are defined here.
"""

from datetime import timedelta

from django.utils import timezone

from . import jobs
from .models import JobRun

# How long the run log is kept. Long enough to look back over a season.
JOB_RUN_RETENTION_DAYS = 180

# Rows deleted per statement, so a large backlog never loads into memory.
PRUNE_BATCH_SIZE = 1000


@jobs.register("prune_job_runs")
def prune_job_runs(*, dry_run):
    """Delete JobRun rows older than JOB_RUN_RETENTION_DAYS."""
    cutoff = timezone.now() - timedelta(days=JOB_RUN_RETENTION_DAYS)
    old = JobRun.objects.filter(started_at__lt=cutoff)

    if dry_run:
        return f"would delete {old.count()} job runs older than {JOB_RUN_RETENTION_DAYS} days"

    deleted = 0
    while True:
        batch = list(old.values_list('pk', flat=True)[:PRUNE_BATCH_SIZE])
        if not batch:
            break
        deleted += JobRun.objects.filter(pk__in=batch).delete()[0]
    return f"deleted {deleted} job runs older than {JOB_RUN_RETENTION_DAYS} days"

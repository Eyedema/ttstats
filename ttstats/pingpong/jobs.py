"""The scheduled-job runner (KAN-37 / T3.6).

Host cron calls one dispatcher, ``manage.py run_scheduled_jobs``, which runs
every job registered here. This module owns the four things every job needs
and no job should re-implement:

* **Overlap protection.** Each job takes a cache lock (``cache.add`` with a
  TTL). A second run that finds the lock held is skipped. The lock is in Redis
  in production; under LocMemCache (dev, tests) it is per process only, which
  is acceptable because nothing runs two dispatchers there.
* **Fail closed.** If the cache cannot be reached, no lock can be taken, so the
  job is skipped rather than run unlocked.
* **A run log.** Every non-dry run writes a ``JobRun`` row: skipped, failed and
  succeeded alike.
* **Failure isolation.** A job that raises is logged with its traceback and
  recorded as FAILED. The exception never reaches the caller, so one broken job
  cannot stop the others or crash the dispatcher.

Not Celery: the VPS has 3.7 GB of RAM, push.py deliberately runs without a
broker, and these jobs run once a day.

Adding a job is one decorator in ``scheduled_jobs.py``::

    @jobs.register("season_rollover")
    def season_rollover(*, dry_run):
        ...
        return "archived 1 season"   # the summary stored on JobRun

A job takes ``dry_run`` (report, write nothing), must be idempotent, must batch
rather than load whole tables, and returns a one-line summary.
"""

import logging
import traceback
import uuid
from dataclasses import dataclass

from django.core.cache import cache
from django.utils import timezone

from . import cache_utils

logger = logging.getLogger(__name__)

# How long a lock outlives a process that died holding it. Longer than any job
# should take; a job that legitimately needs longer passes its own.
DEFAULT_LOCK_TTL = 60 * 60

# The stored traceback is for diagnosis, not archival.
MAX_ERROR_LENGTH = 10_000


@dataclass(frozen=True)
class Job:
    name: str
    func: object
    lock_ttl: int


@dataclass(frozen=True)
class JobResult:
    """What a run did. Returned for dry runs too, which write no JobRun."""

    name: str
    status: str
    summary: str = ''
    error: str = ''
    dry_run: bool = False


_REGISTRY = {}
_loaded = False


def register(name, *, lock_ttl=DEFAULT_LOCK_TTL):
    """Decorator: register ``func(*, dry_run) -> str`` as a scheduled job."""

    def decorator(func):
        if name in _REGISTRY:
            raise ValueError(f"Scheduled job {name!r} is already registered")
        _REGISTRY[name] = Job(name=name, func=func, lock_ttl=lock_ttl)
        return func

    return decorator


def _registry():
    """The registry, after importing the module that holds the real jobs.

    Imported lazily so ``scheduled_jobs`` can import models and this module
    without a cycle at app load.
    """
    global _loaded
    if not _loaded:
        from . import scheduled_jobs  # noqa: F401  (registers on import)

        _loaded = True
    return _REGISTRY


def job_names():
    """Registered job names, in registration (and therefore run) order."""
    return list(_registry())


def run_all(*, dry_run=False):
    return [run_job(name, dry_run=dry_run) for name in job_names()]


def run_job(name, *, dry_run=False):
    """Run one job under its lock. Never raises for a job failure.

    Raises KeyError only for an unknown name, which is a caller bug.
    """
    from .models import JobRun

    job = _registry()[name]

    if dry_run:
        # A dry run writes nothing, so it needs no lock and leaves no JobRun.
        return _execute(job, dry_run=True)

    key = cache_utils.job_lock_key(name)
    token = uuid.uuid4().hex
    try:
        acquired = cache.add(key, token, job.lock_ttl)
    except Exception:
        logger.exception("Scheduled job %s skipped: cache unreachable, no lock", name)
        return _record_skip(name, "cache unreachable; skipped rather than run unlocked")

    if not acquired:
        logger.info("Scheduled job %s skipped: lock held by another run", name)
        return _record_skip(name, "lock held by another run")

    try:
        # Holding the lock means nobody else is running this job, so any row
        # still RUNNING is from a process that died (a container restart).
        JobRun.objects.filter(name=name, status=JobRun.Status.RUNNING).update(
            status=JobRun.Status.INTERRUPTED, finished_at=timezone.now()
        )
        run = JobRun.objects.create(name=name)
        result = _execute(job, dry_run=False)
        run.status = result.status
        run.summary = result.summary
        run.error = result.error
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'summary', 'error', 'finished_at'])
        return result
    finally:
        _release(key, token)


def _execute(job, *, dry_run):
    from .models import JobRun

    try:
        summary = job.func(dry_run=dry_run) or ''
    except Exception as exc:
        logger.exception("Scheduled job %s failed", job.name)
        error = ''.join(traceback.format_exception(exc))[-MAX_ERROR_LENGTH:]
        return JobResult(job.name, JobRun.Status.FAILED, error=error, dry_run=dry_run)
    return JobResult(job.name, JobRun.Status.SUCCESS, summary=str(summary), dry_run=dry_run)


def _record_skip(name, reason):
    from .models import JobRun

    now = timezone.now()
    JobRun.objects.create(
        name=name, status=JobRun.Status.SKIPPED, summary=reason, finished_at=now
    )
    return JobResult(name, JobRun.Status.SKIPPED, summary=reason)


def _release(key, token):
    """Delete the lock only if it is still ours (it may have expired and been
    re-taken by a later run). A failure here is harmless: the TTL frees it."""
    try:
        if cache.get(key) == token:
            cache.delete(key)
    except Exception:  # noqa: BLE001 -- any cache backend error; the lock's TTL frees it anyway
        logger.warning("Could not release job lock %s; it will expire", key)

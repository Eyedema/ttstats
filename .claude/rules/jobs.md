---
paths:
  - "ttstats/pingpong/jobs.py"
  - "ttstats/pingpong/scheduled_jobs.py"
  - "ttstats/pingpong/management/commands/run_scheduled_jobs.py"
  - "deploy/cron/**"
---

# Scheduled jobs

Host cron runs one dispatcher daily at 03:15 UTC:
`manage.py run_scheduled_jobs [--job NAME] [--dry-run] [--list]`. The crontab
is committed in `deploy/cron/ttstats.cron`. Install steps are in its README.

- **Not Celery.** The VPS has 3.7 GB of RAM, and push.py deliberately runs
  without a broker. A broker and workers cost too much for once-a-day jobs.
- **Never run jobs from the deploy step.** The ssh-action timeout has already
  failed deploys.
- **Add a job with one decorator in `scheduled_jobs.py`**:
  `@jobs.register("name")` on `def name(*, dry_run: bool) -> str`. Jobs run in the
  order they are defined. The returned string becomes `JobRun.summary`.
- A job must be **idempotent**, honour `dry_run` (report, write nothing), and
  **batch** its work (`values_list('pk')[:N]` loops or `iterator(chunk_size=)`).
  Never load whole tables.
- `jobs.run_job` owns locking, logging and failure isolation. Don't re-implement
  them in a job:
  - **Lock:** `cache.add(cache_utils.job_lock_key(name), token, ttl)`. A held
    lock skips the job. The key carries no club, because a job runs once per
    deployment. Under LocMemCache the lock only works within one process, which
    is acceptable for dev and tests.
  - **Fail closed:** if the cache is unreachable, the job is skipped, never run
    unlocked.
  - **Run log:** every non-dry run writes a `JobRun` row, whether it was
    skipped, failed or succeeded. A dry run takes no lock and writes no row.
    A row still `running` when the lock is next taken is marked `interrupted`
    (for example, after a container restart).
  - **A failure is logged with its traceback and stored on `JobRun.error`**. It
    never raises to the caller, and the remaining jobs still run.
- Jobs have no viewer, so managers are unfiltered. A per-club job must filter by
  club explicitly.
- A job's pushes go through `notifications.py` and so get the daily limits
  (`push.md`). Make a job's sends at most once through its own idempotent
  state, never through the limiter (`season_rollover` claims
  `SeasonStanding.notified_at`), and send only after the data commits.

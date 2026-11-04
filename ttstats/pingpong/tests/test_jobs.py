"""Tests for the scheduled-job runner (KAN-37 / T3.6).

`jobs.run_job` is what host cron reaches through `run_scheduled_jobs`. These
tests pin its four promises: a held lock means the work does not run, an
unreachable cache means the work does not run (fail closed), a failure is
recorded and never raised, and a dry run writes nothing -- not even a JobRun.
The command-level tests live in test_commands.py.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from pingpong import cache_utils, jobs, scheduled_jobs
from pingpong.models import JobRun

from .conftest import JobRunFactory


@pytest.fixture
def registry(monkeypatch):
    """An empty registry for the test, so real jobs never run by accident."""
    fresh = {}
    monkeypatch.setattr(jobs, "_REGISTRY", fresh)
    monkeypatch.setattr(jobs, "_loaded", True)
    return fresh


def register_recorder(name="recorder", summary="did the thing"):
    """Register a job that records each call, and return the call list."""
    calls = []

    @jobs.register(name)
    def _job(*, dry_run):
        calls.append(dry_run)
        return summary

    return calls


@pytest.mark.django_db
class TestRegister:
    def test_decorator_registers_by_name(self, registry):
        register_recorder("alpha")

        assert jobs.job_names() == ["alpha"]

    def test_duplicate_name_is_rejected(self, registry):
        register_recorder("alpha")

        with pytest.raises(ValueError):
            register_recorder("alpha")

    def test_real_jobs_are_registered(self):
        assert "prune_job_runs" in jobs.job_names()


@pytest.mark.django_db
class TestRunJob:
    def test_success_writes_a_job_run(self, registry):
        calls = register_recorder(summary="pruned 3")

        result = jobs.run_job("recorder")

        assert calls == [False]
        assert result.status == JobRun.Status.SUCCESS
        run = JobRun.objects.get()
        assert run.name == "recorder"
        assert run.status == JobRun.Status.SUCCESS
        assert run.summary == "pruned 3"
        assert run.finished_at is not None
        assert run.finished_at >= run.started_at

    def test_releases_the_lock_afterwards(self, registry):
        register_recorder()

        jobs.run_job("recorder")

        assert cache.get(cache_utils.job_lock_key("recorder")) is None

    def test_held_lock_skips_the_work(self, registry):
        calls = register_recorder()
        cache.add(cache_utils.job_lock_key("recorder"), "someone-else", 60)

        result = jobs.run_job("recorder")

        assert calls == []
        assert result.status == JobRun.Status.SKIPPED
        run = JobRun.objects.get()
        assert run.status == JobRun.Status.SKIPPED
        assert "lock" in run.summary
        # The other holder's lock is left alone.
        assert cache.get(cache_utils.job_lock_key("recorder")) == "someone-else"

    def test_unreachable_cache_fails_closed(self, registry, caplog):
        calls = register_recorder()

        with patch.object(jobs.cache, "add", side_effect=ConnectionError("down")):
            result = jobs.run_job("recorder")

        assert calls == []
        assert result.status == JobRun.Status.SKIPPED
        assert "cache" in JobRun.objects.get().summary
        assert "recorder" in caplog.text

    def test_failure_is_recorded_not_raised(self, registry, caplog):
        @jobs.register("broken")
        def _broken(*, dry_run):
            raise RuntimeError("kaboom")

        result = jobs.run_job("broken")

        assert result.status == JobRun.Status.FAILED
        run = JobRun.objects.get()
        assert run.status == JobRun.Status.FAILED
        assert "kaboom" in run.error
        assert "RuntimeError" in run.error
        assert "broken" in caplog.text
        # A failed job must not leave its lock behind for the TTL.
        assert cache.get(cache_utils.job_lock_key("broken")) is None

    def test_dry_run_passes_the_flag_and_writes_nothing(self, registry):
        calls = register_recorder(summary="would prune 3")

        result = jobs.run_job("recorder", dry_run=True)

        assert calls == [True]
        assert result.status == JobRun.Status.SUCCESS
        assert result.summary == "would prune 3"
        assert result.dry_run is True
        assert JobRun.objects.count() == 0

    def test_unknown_job_raises_key_error(self, registry):
        with pytest.raises(KeyError):
            jobs.run_job("nope")

    def test_interrupted_runs_are_marked_when_the_lock_is_next_taken(self, registry):
        # A container restart mid-job leaves a RUNNING row behind. Whoever
        # next holds the lock knows nobody else is running it.
        register_recorder()
        stale = JobRunFactory(name="recorder", status=JobRun.Status.RUNNING)

        jobs.run_job("recorder")

        stale.refresh_from_db()
        assert stale.status == JobRun.Status.INTERRUPTED
        assert stale.finished_at is not None


@pytest.mark.django_db
class TestRunAll:
    def test_runs_every_job_in_registration_order(self, registry):
        order = []

        @jobs.register("first")
        def _first(*, dry_run):
            order.append("first")
            return ""

        @jobs.register("second")
        def _second(*, dry_run):
            order.append("second")
            return ""

        results = jobs.run_all()

        assert order == ["first", "second"]
        assert [r.name for r in results] == ["first", "second"]

    def test_one_failure_does_not_stop_the_rest(self, registry):
        @jobs.register("broken")
        def _broken(*, dry_run):
            raise RuntimeError("kaboom")

        calls = register_recorder("after")

        results = jobs.run_all()

        assert [r.status for r in results] == [
            JobRun.Status.FAILED, JobRun.Status.SUCCESS,
        ]
        assert calls == [False]


@pytest.mark.django_db
class TestPruneJobRuns:
    def _aged(self, days):
        return JobRunFactory(started_at=timezone.now() - timedelta(days=days))

    def test_deletes_only_rows_past_retention(self):
        old = self._aged(scheduled_jobs.JOB_RUN_RETENTION_DAYS + 1)
        recent = self._aged(scheduled_jobs.JOB_RUN_RETENTION_DAYS - 1)

        summary = scheduled_jobs.prune_job_runs(dry_run=False)

        assert not JobRun.objects.filter(pk=old.pk).exists()
        assert JobRun.objects.filter(pk=recent.pk).exists()
        assert "1" in summary

    def test_dry_run_deletes_nothing(self):
        old = self._aged(scheduled_jobs.JOB_RUN_RETENTION_DAYS + 1)

        summary = scheduled_jobs.prune_job_runs(dry_run=True)

        assert JobRun.objects.filter(pk=old.pk).exists()
        assert "would" in summary.lower()

    def test_is_idempotent(self):
        self._aged(scheduled_jobs.JOB_RUN_RETENTION_DAYS + 1)

        scheduled_jobs.prune_job_runs(dry_run=False)
        remaining = JobRun.objects.count()
        scheduled_jobs.prune_job_runs(dry_run=False)

        assert JobRun.objects.count() == remaining

    def test_deletes_in_batches(self, monkeypatch):
        monkeypatch.setattr(scheduled_jobs, "PRUNE_BATCH_SIZE", 2)
        for _ in range(5):
            self._aged(scheduled_jobs.JOB_RUN_RETENTION_DAYS + 1)

        summary = scheduled_jobs.prune_job_runs(dry_run=False)

        assert JobRun.objects.count() == 0
        assert "5" in summary

"""KAN-111: Sentry cron check-ins for the nightly jobs, and trace sampling.

The SDK is never initialised in tests (prod.py only), so these patch
`sentry_sdk` and assert what would have been sent. With no active client,
nothing is called at all.
"""
from unittest import mock

import pytest
from django.core.management import call_command

from pingpong import jobs
from ttstats import sentry


@pytest.fixture
def sdk(monkeypatch):
    """An 'active' SDK whose check-ins are recorded."""
    calls = []

    def capture_checkin(**kwargs):
        calls.append(kwargs)
        return "checkin-1"

    monkeypatch.setattr(sentry, "_sdk_active", lambda: True)
    monkeypatch.setattr("sentry_sdk.crons.capture_checkin", capture_checkin)
    return calls


@pytest.fixture
def registry(monkeypatch):
    """A private job registry, so the real jobs never run here."""
    reg = {}
    monkeypatch.setattr(jobs, "_REGISTRY", reg)
    monkeypatch.setattr(jobs, "_registry", lambda: reg)
    return reg


def _job(registry, name, func):
    registry[name] = jobs.Job(name=name, func=func, lock_ttl=60)


class TestCronMonitor:
    def test_inert_without_an_active_client(self, monkeypatch):
        monkeypatch.setattr(sentry, "_sdk_active", lambda: False)
        with mock.patch("sentry_sdk.crons.capture_checkin") as capture:
            with sentry.cron_monitor("x", {"schedule": {}}) as checkin:
                checkin.ok = True
        capture.assert_not_called()

    def test_in_progress_then_ok(self, sdk):
        with sentry.cron_monitor("nightly", {"schedule": {"type": "crontab"}}) as checkin:
            checkin.ok = True

        assert [c["status"] for c in sdk] == ["in_progress", "ok"]
        assert sdk[0]["monitor_slug"] == "nightly"
        assert sdk[0]["monitor_config"] == {"schedule": {"type": "crontab"}}
        assert sdk[1]["check_in_id"] == "checkin-1"
        assert sdk[1]["duration"] >= 0

    def test_not_ok_is_an_error(self, sdk):
        with sentry.cron_monitor("nightly", {}) as checkin:
            checkin.ok = False
        assert sdk[-1]["status"] == "error"

    def test_an_exception_is_an_error_and_still_raises(self, sdk):
        with pytest.raises(ZeroDivisionError):
            with sentry.cron_monitor("nightly", {}):
                1 / 0
        assert sdk[-1]["status"] == "error"

    def test_disabled_sends_nothing(self, sdk):
        with sentry.cron_monitor("nightly", {}, enabled=False) as checkin:
            checkin.ok = True
        assert sdk == []

    def test_a_failing_sdk_never_breaks_the_job(self, monkeypatch):
        monkeypatch.setattr(sentry, "_sdk_active", lambda: True)
        ran = []
        with mock.patch("sentry_sdk.crons.capture_checkin", side_effect=RuntimeError):
            with sentry.cron_monitor("nightly", {}) as checkin:
                ran.append(1)
                checkin.ok = True
        assert ran == [1]


@pytest.mark.django_db
class TestScheduledJobsCheckIn:
    def test_full_run_checks_in_ok(self, sdk, registry):
        _job(registry, "fine", lambda *, dry_run: "did it")
        call_command("run_scheduled_jobs")

        assert [c["status"] for c in sdk] == ["in_progress", "ok"]
        assert sdk[0]["monitor_slug"] == sentry.SCHEDULED_JOBS_MONITOR
        config = sdk[0]["monitor_config"]
        assert config["schedule"] == {"type": "crontab", "value": "15 3 * * *"}
        assert config["timezone"] == "UTC"

    def test_a_failed_job_makes_the_run_an_error(self, sdk, registry):
        def broken(*, dry_run):
            raise RuntimeError("nope")

        _job(registry, "broken", broken)
        _job(registry, "fine", lambda *, dry_run: "ok")
        call_command("run_scheduled_jobs")

        assert sdk[-1]["status"] == "error"

    def test_a_skipped_job_is_an_error_too(self, sdk, registry, monkeypatch):
        """A skip means the cache was down or a run overlapped: worth a look."""
        _job(registry, "fine", lambda *, dry_run: "ok")
        monkeypatch.setattr(jobs.cache, "add", lambda *a, **k: False)
        call_command("run_scheduled_jobs")

        assert sdk[-1]["status"] == "error"

    @pytest.mark.parametrize("options", [{"dry_run": True}, {"job": "fine"}])
    def test_hand_runs_do_not_check_in(self, sdk, registry, options):
        _job(registry, "fine", lambda *, dry_run: "ok")
        call_command("run_scheduled_jobs", **options)
        assert sdk == []


class TestTraceSampling:
    def _env(self, path, query=""):
        return {"wsgi_environ": {"PATH_INFO": path, "QUERY_STRING": query}}

    def test_static_and_worker_files_are_never_traced(self):
        sampler = sentry.make_traces_sampler(0.5, 0.05, live_rate=1.0)
        for path in ("/static/pingpong/css/app.abc123def456.css", "/sw.js",
                     "/manifest.webmanifest", "/healthz"):
            assert sampler(self._env(path)) == 0, path

    def test_live_writes_get_the_live_rate(self):
        sampler = sentry.make_traces_sampler(0.1, 0.01, live_rate=1.0)
        for op in ("point", "undo", "sync", "start", "side-switch"):
            assert sampler(self._env(f"/pingpong/matches/7/live/{op}/")) == 1.0, op

    def test_a_held_long_poll_is_not_traced(self):
        """Held for up to 20 s by design: it would own every latency alert."""
        sampler = sentry.make_traces_sampler(0.1, 0.01, live_rate=1.0)
        assert sampler(self._env("/pingpong/matches/7/live/state/", "since=3&wait=1")) == 0
        assert sampler(self._env("/pingpong/matches/7/live/state/", "since=3")) == 0.1

    def test_pages_get_the_base_rate(self):
        sampler = sentry.make_traces_sampler(0.1, 0.01, live_rate=1.0)
        assert sampler(self._env("/pingpong/matches/7/live/")) == 0.1  # the page itself
        assert sampler(self._env("/pingpong/")) == 0.1

    def test_live_rate_defaults_off_when_tracing_is_off(self, monkeypatch):
        import sentry_sdk

        captured = {}
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
        monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
        monkeypatch.delenv("SENTRY_TRACES_SAMPLE_RATE", raising=False)
        monkeypatch.delenv("SENTRY_LIVE_TRACES_SAMPLE_RATE", raising=False)
        sentry.init_sentry()
        sampler = captured["traces_sampler"]
        assert sampler(self._env("/pingpong/matches/7/live/point/")) == 0

    def test_live_rate_is_one_when_tracing_is_on(self, monkeypatch):
        import sentry_sdk

        captured = {}
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
        monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")
        monkeypatch.delenv("SENTRY_LIVE_TRACES_SAMPLE_RATE", raising=False)
        sentry.init_sentry()
        sampler = captured["traces_sampler"]
        assert sampler(self._env("/pingpong/matches/7/live/point/")) == 1.0

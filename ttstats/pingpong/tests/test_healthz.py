"""`/healthz` (KAN-109): the probe Docker, the deploy smoke test, the uptime
monitor and the offline back-online check (snapshot_mode.js) all hit.

It is answered by `HealthzMiddleware`, first in `MIDDLEWARE`, so it runs
before host validation, the HTTPS redirect, sessions, auth and tenancy: a
probe from inside the container (plain HTTP, Host 127.0.0.1) must get the
same answer as one through the proxy.
"""
from unittest import mock

import pytest
from django.conf import settings
from django.core.cache.backends.locmem import LocMemCache
from django.db import DatabaseError
from django.test import override_settings

from ttstats import healthz


@pytest.fixture(autouse=True)
def _fresh_migration_memo():
    healthz.reset_migration_memo()
    yield
    healthz.reset_migration_memo()


@pytest.mark.django_db
class TestHealthy:
    def test_ok_with_release_and_checks(self, client, monkeypatch):
        monkeypatch.setenv("SENTRY_RELEASE", "abc123")
        response = client.get("/healthz")

        assert response.status_code == 200
        assert response["Content-Type"] == "application/json"
        assert response.json() == {
            "status": "ok",
            "release": "abc123",
            "checks": {"db": "ok", "cache": "ok", "migrations": "ok"},
        }

    def test_release_is_null_when_unset(self, client, monkeypatch):
        monkeypatch.delenv("SENTRY_RELEASE", raising=False)
        assert client.get("/healthz").json()["release"] is None

    def test_trailing_slash_is_the_same_endpoint(self, client):
        assert client.get("/healthz/").status_code == 200

    def test_head_answers_without_a_body(self, client):
        response = client.head("/healthz")
        assert response.status_code == 200
        assert response.content == b""

    def test_never_cached(self, client):
        cache_control = client.get("/healthz")["Cache-Control"]
        assert "no-store" in cache_control
        assert "max-age=0" in cache_control

    def test_sets_no_cookies(self, client):
        response = client.get("/healthz")
        assert not response.cookies

    def test_only_get_and_head(self, client):
        assert client.post("/healthz").status_code == 405

    def test_one_query_once_migrations_are_known_clean(self, client, django_assert_num_queries):
        client.get("/healthz")  # primes the per-process migration memo
        with django_assert_num_queries(1):
            assert client.get("/healthz").status_code == 200

    def test_unauthenticated_needs_no_login_or_club(self, client):
        # No user, no player, no club: base.html and the tenancy middleware
        # never run.
        assert client.get("/healthz").status_code == 200


@pytest.mark.django_db
class TestFailures:
    def test_database_down_is_503(self, client):
        with mock.patch.object(healthz, "_check_db", side_effect=DatabaseError("boom")):
            response = client.get("/healthz")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "error"
        assert body["checks"]["db"] == "error"
        # Which check failed, never why: no exception text leaves the process.
        assert "boom" not in response.content.decode()

    def test_cache_down_is_tolerated(self, client):
        """Redis down degrades the app (sessions fall back to the DB, caches
        miss) but does not stop it serving, so it must not fail the probe that
        decides rollbacks."""
        with mock.patch.object(LocMemCache, "set", side_effect=ConnectionError("redis")):
            response = client.get("/healthz")

        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["checks"]["cache"] == "error"

    def test_cache_round_trip_mismatch_is_degraded(self, client):
        with mock.patch.object(LocMemCache, "get", return_value=None):
            assert client.get("/healthz").json()["checks"]["cache"] == "error"

    def test_a_hung_cache_cannot_hold_the_probe(self, client, monkeypatch):
        import threading
        import time

        release = threading.Event()
        monkeypatch.setattr(healthz, "CACHE_BUDGET_SECONDS", 0.05)
        monkeypatch.setattr(healthz, "_check_cache", lambda: release.wait(5))
        started = time.monotonic()
        try:
            response = client.get("/healthz")
        finally:
            release.set()

        assert time.monotonic() - started < 1
        assert response.status_code == 200
        assert response.json()["checks"]["cache"] == "timeout"
        assert response.json()["status"] == "degraded"

    def test_pending_migration_is_503(self, client):
        with mock.patch.object(healthz, "_pending_migrations", return_value=True):
            response = client.get("/healthz")

        assert response.status_code == 503
        assert response.json()["checks"]["migrations"] == "pending"

    def test_pending_is_rechecked_but_clean_is_remembered(self, client, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(healthz.time, "monotonic", lambda: clock[0])
        with mock.patch.object(healthz, "_pending_migrations", return_value=True) as plan:
            client.get("/healthz")
            client.get("/healthz")
            assert plan.call_count == 1  # within the window: the memo answers
            clock[0] += healthz.PENDING_RECHECK_SECONDS
            assert client.get("/healthz").status_code == 503
        assert plan.call_count == 2  # pending: asked again after the window

        clock[0] += healthz.PENDING_RECHECK_SECONDS
        with mock.patch.object(healthz, "_pending_migrations", return_value=False) as plan:
            client.get("/healthz")
            client.get("/healthz")
        assert plan.call_count == 1  # clean: remembered for the process

    def test_migration_check_failure_is_an_error_not_a_crash(self, client):
        with mock.patch.object(healthz, "_pending_migrations", side_effect=DatabaseError("x")):
            response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["checks"]["migrations"] == "error"


@pytest.mark.django_db
class TestBypassesTheStack:
    @override_settings(SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=["pingpong.example.org"])
    def test_plain_http_from_an_unlisted_host(self, client):
        """Docker's probe: http://127.0.0.1:8000/healthz, no proxy headers."""
        response = client.get("/healthz", HTTP_HOST="127.0.0.1:8000")
        assert response.status_code == 200

    def test_is_the_first_middleware(self):
        assert settings.MIDDLEWARE[0] == "ttstats.healthz.HealthzMiddleware"

    def test_other_paths_pass_through(self, client):
        assert client.get("/healthzz").status_code == 404
        assert client.get("/pingpong/healthz").status_code == 404


class TestTraceSampling:
    def test_healthz_is_never_traced(self):
        from ttstats import sentry

        sampler = sentry.make_traces_sampler(1.0, 1.0)
        assert sampler({"wsgi_environ": {"PATH_INFO": "/healthz"}}) == 0
        assert sampler({"wsgi_environ": {"PATH_INFO": "/healthz/"}}) == 0


class TestProdSettings:
    def test_still_first_after_prod_inserts_its_own_middleware(self):
        """prod.py inserts CSP at index 1 and Sentry's 4xx middleware after
        WhiteNoise; neither may land in front of the probe."""
        import json
        import os
        import subprocess
        import sys

        from pingpong.tests.test_prod_settings import PROJECT_DIR, REQUIRED_ENV

        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        env.update(REQUIRED_ENV)
        out = subprocess.run(
            [sys.executable, "-c",
             "import json; from ttstats.settings import prod; print(json.dumps(prod.MIDDLEWARE))"],
            cwd=PROJECT_DIR, env=env, capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        middleware = json.loads(out.stdout.strip().splitlines()[-1])
        assert middleware[0] == "ttstats.healthz.HealthzMiddleware"

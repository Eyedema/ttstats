"""KAN-165 (server part) + KAN-168 -- ``GET .../live/state/?since=<rev>[&wait=1]``.

The other phone's points reach this one through a held GET: the server
answers at once when ``rev`` moved, 204 when it did not and the client did
not ask to wait, and otherwise holds the request, reading one cache key
(``cache_utils.live_rev_key``) every ``live_longpoll.TICK`` until an op
commits or ``LIVE_LONGPOLL_WAIT`` runs out.

The waiting loop itself is ``live_longpoll.wait_for_change``: pure, no
Django, tested here without a DB and with a fake clock.
"""
import json
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pingpong import cache_utils, live_longpoll
from pingpong.models import LiveScorer, Match

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    PlayerFactory,
    UserFactory,
    live_match,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# The pure loop
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _wait(since, cache_values, *, db_values=None, wait=1.0, tick=0.2, db_every=2.0):
    """Run the loop on a fake clock; the cache returns ``cache_values`` in
    order (the last one repeats)."""
    clock = FakeClock()
    reads = {"cache": 0, "db": 0}

    def read_cache():
        reads["cache"] += 1
        return cache_values[min(reads["cache"] - 1, len(cache_values) - 1)]

    read_db = None
    if db_values is not None:
        def read_db():
            reads["db"] += 1
            return db_values[min(reads["db"] - 1, len(db_values) - 1)]

    result = live_longpoll.wait_for_change(
        since,
        read_cache=read_cache,
        read_db=read_db,
        wait=wait,
        tick=tick,
        db_every=db_every,
        clock=clock.clock,
        sleep=clock.sleep,
    )
    return result, clock, reads


class TestWaitForChange:
    def test_returns_the_new_rev_as_soon_as_the_cache_moves(self):
        result, clock, reads = _wait(3, [3, 3, 4])
        assert result == 4
        assert reads["cache"] == 3
        assert clock.now == pytest.approx(0.6)

    def test_times_out_with_none(self):
        result, clock, _ = _wait(3, [3], wait=1.0)
        assert result is None
        assert clock.now == pytest.approx(1.0)

    def test_never_sleeps_past_the_deadline(self):
        _, clock, _ = _wait(3, [3], wait=0.5, tick=0.2)
        assert clock.now == pytest.approx(0.5)
        assert clock.sleeps[-1] == pytest.approx(0.1)

    def test_a_missing_key_is_not_a_change(self):
        """An evicted or flushed key reads None: keep waiting, never wake."""
        result, _, _ = _wait(3, [None], wait=1.0)
        assert result is None

    def test_without_a_db_reader_the_db_is_never_read(self):
        _, _, reads = _wait(3, [3], wait=5.0)
        assert reads["db"] == 0

    def test_the_db_fallback_runs_every_db_every_seconds(self):
        result, clock, reads = _wait(3, [3], db_values=[3, 5], wait=10.0, db_every=2.0)
        assert result == 5
        assert reads["db"] == 2
        assert clock.now == pytest.approx(4.0)

    def test_the_db_fallback_also_times_out(self):
        result, _, reads = _wait(3, [3], db_values=[3], wait=5.0, db_every=2.0)
        assert result is None
        assert reads["db"] == 2


# ---------------------------------------------------------------------------
# Cache keys
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestKeys:
    def test_flag_off_keys_have_no_club(self):
        assert cache_utils.live_rev_key(7) == "live_rev_7"
        assert cache_utils.live_poll_key(7, 3) == "live_poll_7_3"

    def test_keys_carry_the_viewers_club(self, settings):
        from ttstats.middleware import _thread_locals

        settings.FEATURE_FLAGS = {"clubs": True}
        club = ClubFactory()
        user = UserFactory()
        _thread_locals.user = user
        _thread_locals.club = club
        try:
            assert cache_utils.live_rev_key(7) == f"club{club.pk}:live_rev_7"
            assert cache_utils.live_poll_key(7, 3) == f"club{club.pk}:live_poll_7_3"
        finally:
            del _thread_locals.user
            del _thread_locals.club

    def test_locmem_is_not_a_shared_cache(self):
        assert cache_utils.cache_is_shared() is False

    def test_redis_is_a_shared_cache(self, settings):
        settings.CACHES = {
            "default": {"BACKEND": "django_redis.cache.RedisCache", "LOCATION": "redis://x"}
        }
        assert cache_utils.cache_is_shared() is True


# ---------------------------------------------------------------------------
# Publishing rev on commit
# ---------------------------------------------------------------------------


def _url(match, **params):
    url = reverse("pingpong:live_state", args=[match.pk])
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return url


def _post(client, name, match, body):
    return client.post(
        reverse(f"pingpong:{name}", args=[match.pk]),
        data=json.dumps(body),
        content_type="application/json",
    )


def _host_client(match):
    host = LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST).player
    client = Client()
    client.force_login(host.user)
    return client


@pytest.mark.django_db
class TestRevIsPublishedOnCommit:
    def test_an_applied_op_sets_the_key_after_commit(
        self, django_capture_on_commit_callbacks
    ):
        match = live_match(started=True)
        client = _host_client(match)
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            assert _post(client, "live_point", match, {"side": "team1", "seq": 1}).status_code == 200
        # Not before the commit: a waiter must never wake to uncommitted data.
        assert cache.get(cache_utils.live_rev_key(match.pk)) is None
        for callback in callbacks:
            callback()
        assert cache.get(cache_utils.live_rev_key(match.pk)) == 1

    def test_a_batch_publishes_once_with_the_final_rev(
        self, django_capture_on_commit_callbacks
    ):
        match = live_match(started=True)
        client = _host_client(match)
        ops = [{"seq": i, "type": "point", "side": "team1"} for i in range(1, 5)]
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            assert _post(client, "live_sync", match, {"ops": ops}).status_code == 200
        assert len(callbacks) == 1
        assert cache.get(cache_utils.live_rev_key(match.pk)) == 4

    def test_a_rule_rejection_publishes_nothing(
        self, django_capture_on_commit_callbacks
    ):
        match = live_match(started=False)
        client = _host_client(match)
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            # No server chosen yet: the point is refused (409), rev stays.
            _post(client, "live_point", match, {"side": "team1", "seq": 1})
        assert callbacks == []

    def test_a_dead_cache_does_not_fail_the_op(
        self, django_capture_on_commit_callbacks, monkeypatch
    ):
        match = live_match(started=True)
        client = _host_client(match)

        def boom(*args, **kwargs):
            raise ConnectionError("redis down")

        monkeypatch.setattr(cache, "set", boom)
        with django_capture_on_commit_callbacks(execute=True):
            response = _post(client, "live_point", match, {"side": "team1", "seq": 1})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# The endpoint: instant answers (KAN-165)
# ---------------------------------------------------------------------------


@pytest.fixture
def short_wait(settings, monkeypatch):
    """Keep held requests at about a second, with a fast tick."""
    settings.LIVE_LONGPOLL_WAIT = 1
    monkeypatch.setattr(live_longpoll, "TICK", 0.05)
    return settings


@pytest.mark.django_db
class TestSince:
    def test_no_since_is_the_full_payload(self):
        match = live_match(started=True)
        response = _host_client(match).get(_url(match))
        assert response.status_code == 200
        assert response.json()["rev"] == 0

    def test_a_different_rev_answers_at_once(self, short_wait):
        match = live_match(started=True)
        Match.all_objects.filter(pk=match.pk).update(live_rev=4)
        started = time.monotonic()
        response = _host_client(match).get(_url(match, since=3, wait=1))
        assert time.monotonic() - started < 0.5
        assert response.status_code == 200
        assert response.json()["rev"] == 4

    def test_unchanged_without_wait_is_204_with_no_body(self):
        match = live_match(started=True)
        response = _host_client(match).get(_url(match, since=0))
        assert response.status_code == 204
        assert response.content == b""

    def test_wait_zero_is_an_instant_answer(self):
        match = live_match(started=True)
        response = _host_client(match).get(_url(match, since=0, wait=0))
        assert response.status_code == 204

    @pytest.mark.parametrize("since", ["abc", "1.5", "", "true"])
    def test_a_non_int_since_is_ignored(self, since):
        match = live_match(started=True)
        response = _host_client(match).get(_url(match, since=since))
        assert response.status_code == 200
        assert response.json()["rev"] == 0

    def test_unchanged_still_marks_the_scorer_seen(self):
        match = live_match(started=True)
        host = LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST)
        long_ago = timezone.now() - timedelta(hours=1)
        LiveScorer.objects.filter(pk=host.pk).update(last_seen_at=long_ago)
        assert _host_client(match).get(_url(match, since=0)).status_code == 204
        host.refresh_from_db()
        assert host.last_seen_at > long_ago

    def test_a_finished_match_is_never_held(self, short_wait):
        match = live_match(started=True)
        Match.all_objects.filter(pk=match.pk).update(is_live=False, live_state=None)
        started = time.monotonic()
        response = _host_client(match).get(_url(match, since=0, wait=1))
        assert time.monotonic() - started < 0.5
        assert response.status_code == 204


# ---------------------------------------------------------------------------
# The endpoint: held requests (KAN-168)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHeld:
    def test_times_out_with_204(self, short_wait):
        match = live_match(started=True)
        started = time.monotonic()
        response = _host_client(match).get(_url(match, since=0, wait=1))
        elapsed = time.monotonic() - started
        assert response.status_code == 204
        assert response.content == b""
        assert 0.9 <= elapsed < 2.0

    def test_wakes_on_the_cache_key_and_answers_the_payload(self, short_wait, monkeypatch):
        match = live_match(started=True)
        key = cache_utils.live_rev_key(match.pk)
        ticks = {"n": 0}
        real_sleep = live_longpoll.time.sleep

        def sleep(seconds):
            # An op commits elsewhere during the third tick.
            ticks["n"] += 1
            if ticks["n"] == 3:
                Match.all_objects.filter(pk=match.pk).update(live_rev=1)
                cache.set(key, 1)
            real_sleep(seconds)

        monkeypatch.setattr(live_longpoll.time, "sleep", sleep)
        response = _host_client(match).get(_url(match, since=0, wait=1))
        assert response.status_code == 200
        assert response.json()["rev"] == 1
        assert ticks["n"] == 3

    def test_no_db_queries_inside_the_loop_with_a_shared_cache(
        self, short_wait, monkeypatch
    ):
        """A held call costs exactly the queries of an instant 204: the loop
        reads only the cache key."""
        monkeypatch.setattr(cache_utils, "cache_is_shared", lambda: True)
        match = live_match(started=True)
        client = _host_client(match)
        client.get(_url(match, since=0))  # stamp last_seen_at out of the way

        with CaptureQueriesContext(connection) as instant:
            assert client.get(_url(match, since=0)).status_code == 204
        with CaptureQueriesContext(connection) as held:
            assert client.get(_url(match, since=0, wait=1)).status_code == 204
        assert len(held.captured_queries) == len(instant.captured_queries)

    def test_without_a_shared_cache_the_db_is_the_fallback(self, short_wait, monkeypatch):
        """LocMemCache is per process: an op on another worker never reaches
        this one's key, so the loop re-reads ``live_rev`` from the DB."""
        monkeypatch.setattr(live_longpoll, "DB_EVERY", 0.2)
        match = live_match(started=True)
        Match.all_objects.filter(pk=match.pk).update(live_rev=0)
        ticks = {"n": 0}
        real_sleep = live_longpoll.time.sleep

        def sleep(seconds):
            ticks["n"] += 1
            if ticks["n"] == 2:
                # Committed by "another worker": the DB moves, the key doesn't.
                Match.all_objects.filter(pk=match.pk).update(live_rev=2)
            real_sleep(seconds)

        monkeypatch.setattr(live_longpoll.time, "sleep", sleep)
        response = _host_client(match).get(_url(match, since=0, wait=1))
        assert response.status_code == 200
        assert response.json()["rev"] == 2

    def test_a_second_concurrent_wait_is_204_at_once(self, short_wait):
        match = live_match(started=True)
        client = _host_client(match)
        host = LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST).player
        # The first held request's marker, as it would be while it waits.
        cache.set(cache_utils.live_poll_key(match.pk, host.user.pk), 1, 30)
        started = time.monotonic()
        response = client.get(_url(match, since=0, wait=1))
        assert time.monotonic() - started < 0.5
        assert response.status_code == 204
        assert response["X-Longpoll"] == "busy"

    def test_the_marker_is_released_after_the_wait(self, short_wait):
        match = live_match(started=True)
        client = _host_client(match)
        host = LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST).player
        client.get(_url(match, since=0, wait=1))
        assert cache.get(cache_utils.live_poll_key(match.pk, host.user.pk)) is None

    def test_another_user_has_their_own_marker(self, short_wait):
        match = live_match(started=True, shared=True)
        host = LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST).player
        guest = LiveScorer.objects.get(match=match, role=LiveScorer.Role.GUEST).player
        cache.set(cache_utils.live_poll_key(match.pk, host.user.pk), 1, 30)
        client = Client()
        client.force_login(guest.user)
        started = time.monotonic()
        response = client.get(_url(match, since=0, wait=1))
        assert time.monotonic() - started >= 0.9
        assert response.status_code == 204
        assert "X-Longpoll" not in response

    def test_closes_the_db_connection_before_waiting(self, short_wait, monkeypatch):
        """Outside a transaction (prod), a held request gives its connection
        back. Inside one (this test) closing would break the transaction, so
        the view leaves it alone; both branches are pinned."""
        from pingpong import views

        closed = []
        monkeypatch.setattr(views, "_release_db_connection", lambda: closed.append(True))
        match = live_match(started=True)
        _host_client(match).get(_url(match, since=0, wait=1))
        assert closed == [True]

    def test_release_skips_an_atomic_block(self, monkeypatch):
        from pingpong import views

        calls = []
        monkeypatch.setattr(connection, "close", lambda: calls.append(True))
        # pytest-django wraps this test in a transaction.
        views._release_db_connection()
        assert calls == []


@pytest.mark.django_db
class TestHeldPollCap:
    """At most ``LIVE_LONGPOLL_MAX_HELD`` held polls per process, so held
    phones can never take every gunicorn thread and starve the ops."""

    @pytest.fixture
    def one_slot(self, short_wait):
        short_wait.LIVE_LONGPOLL_MAX_HELD = 1
        from pingpong import views

        return views._held_polls()

    def test_full_is_busy_at_once(self, one_slot):
        match = live_match(started=True)
        host = LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST).player
        assert one_slot.acquire(blocking=False)  # another phone's held poll
        try:
            started = time.monotonic()
            response = _host_client(match).get(_url(match, since=0, wait=1))
            assert time.monotonic() - started < 0.5
            assert response.status_code == 204
            assert response["X-Longpoll"] == "busy"
            # Its per-user marker is not left behind to refuse the retry.
            assert cache.get(cache_utils.live_poll_key(match.pk, host.user.pk)) is None
        finally:
            one_slot.release()

    def test_the_slot_is_released_after_a_held_poll(self, one_slot):
        match = live_match(started=True)
        client = _host_client(match)
        for _ in range(2):
            response = client.get(_url(match, since=0, wait=1))
            assert response.status_code == 204 and "X-Longpoll" not in response
        assert one_slot.acquire(blocking=False)
        one_slot.release()

    def test_the_slot_is_released_when_the_wait_raises(self, one_slot, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("cache down")

        monkeypatch.setattr(live_longpoll, "wait_for_change", boom)
        match = live_match(started=True)
        with pytest.raises(RuntimeError):
            _host_client(match).get(_url(match, since=0, wait=1))
        assert one_slot.acquire(blocking=False)
        one_slot.release()

    def test_instant_answers_take_no_slot(self, one_slot):
        match = live_match(started=True)
        assert one_slot.acquire(blocking=False)
        try:
            client = _host_client(match)
            assert client.get(_url(match)).status_code == 200
            assert client.get(_url(match, since=0)).status_code == 204
        finally:
            one_slot.release()

    def test_default_leaves_threads_for_the_ops(self, settings):
        """Default: GUNICORN_THREADS (8) - 3, so three threads per process
        stay free for ops and pages whatever the phones do."""
        from django.conf import settings as live

        assert 1 <= live.LIVE_LONGPOLL_MAX_HELD < 8


@pytest.mark.django_db
class TestRefusedWithoutWaiting:
    def test_stranger_is_403_at_once(self, short_wait):
        short_wait.LIVE_LONGPOLL_WAIT = 5
        match = live_match(started=True)
        stranger = PlayerFactory(with_user=True)
        client = Client()
        client.force_login(stranger.user)
        started = time.monotonic()
        response = client.get(_url(match, since=0, wait=1))
        assert response.status_code == 403
        assert time.monotonic() - started < 0.5

    def test_other_club_is_404_at_once(self, short_wait, settings):
        short_wait.LIVE_LONGPOLL_WAIT = 5
        settings.FEATURE_FLAGS = {"clubs": True}
        home, away = ClubFactory(), ClubFactory()
        match = live_match(started=True, shared=True, club=away)
        guest = LiveScorer.objects.get(match=match, role=LiveScorer.Role.GUEST).player
        ClubMembershipFactory(club=home, user=guest.user)
        client = Client()
        client.force_login(guest.user)
        started = time.monotonic()
        response = client.get(_url(match, since=0, wait=1))
        assert response.status_code == 404
        assert time.monotonic() - started < 0.5

    def test_anonymous_is_redirected_at_once(self, short_wait):
        short_wait.LIVE_LONGPOLL_WAIT = 5
        match = live_match(started=True)
        started = time.monotonic()
        response = Client().get(_url(match, since=0, wait=1))
        assert response.status_code == 302
        assert time.monotonic() - started < 0.5


# ---------------------------------------------------------------------------
# Two threads: an op committed on one wakes the other (acceptance criterion)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
class TestAcrossThreads:
    def test_answers_within_a_tick_of_an_op_committed_in_another_thread(
        self, settings, monkeypatch
    ):
        settings.LIVE_LONGPOLL_WAIT = 5
        # Rule out the DB fallback: only the cache key can wake it in time.
        monkeypatch.setattr(live_longpoll, "DB_EVERY", 60)
        match = live_match(started=True, shared=True)
        host = LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST).player
        guest = LiveScorer.objects.get(match=match, role=LiveScorer.Role.GUEST).player
        waiter, scorer = Client(), Client()
        waiter.force_login(host.user)
        scorer.force_login(guest.user)

        committed_at = {}

        def score():
            time.sleep(0.4)
            response = _post(scorer, "live_point", match, {"side": "team2", "seq": 1})
            committed_at["t"] = time.monotonic()
            committed_at["status"] = response.status_code
            connection.close()

        thread = threading.Thread(target=score)
        thread.start()
        response = waiter.get(_url(match, since=0, wait=1))
        answered_at = time.monotonic()
        thread.join()

        assert committed_at["status"] == 200
        assert response.status_code == 200
        assert response.json()["rev"] == 1
        # One TICK (200 ms) plus the re-read, with slack for a loaded CI box.
        assert answered_at - committed_at["t"] < 0.6


# ---------------------------------------------------------------------------
# The client is told its abort timeout; it never types it
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScoreboardConfig:
    def test_bootstrap_carries_the_longpoll_abort_timeout(self, settings):
        settings.LIVE_LONGPOLL_WAIT = 20
        match = live_match(started=True)
        response = _host_client(match).get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        config = response.context["scoreboard_config"]
        assert config["longpoll_abort_ms"] == (20 + live_longpoll.ABORT_SLACK) * 1000
        assert live_longpoll.ABORT_SLACK == 5


# ---------------------------------------------------------------------------
# The worker model the long-poll depends on
# ---------------------------------------------------------------------------


class TestGunicornConfig:
    """A held request must take a thread, not one of three processes, and
    must answer before gunicorn's timeout."""

    def _flags(self, path):
        text = (REPO_ROOT / path).read_text()
        return text, re.search(r"--timeout (\d+)", text)

    def test_prod_runs_threaded_workers_from_env(self):
        text, _ = self._flags("docker/django/entrypoint.sh")
        assert "--worker-class gthread" in text
        assert '--workers "${GUNICORN_WORKERS:-3}"' in text
        assert '--threads "${GUNICORN_THREADS:-8}"' in text

    def test_a_saturated_worker_stops_accepting(self):
        """gthread accepts up to --worker-connections (default 1000) and
        queues them behind busy threads; equal to the thread count, a full
        worker leaves new connections to the others."""
        text, _ = self._flags("docker/django/entrypoint.sh")
        assert (
            '--worker-connections "${GUNICORN_WORKER_CONNECTIONS:-${GUNICORN_THREADS:-8}}"'
            in text
        )

    def test_gunicorn_is_pinned(self):
        """The gthread accept/keep-alive behaviour above was read from this
        version's source; an unpinned build could change it."""
        lines = (REPO_ROOT / "requirements.txt").read_text().splitlines()
        assert any(re.fullmatch(r"gunicorn==\d+\.\d+\.\d+", line.strip()) for line in lines)

    def test_e2e_runs_the_same_worker_class(self):
        text, _ = self._flags("scripts/e2e_server.sh")
        assert "--worker-class gthread" in text

    @pytest.mark.parametrize("path", ["docker/django/entrypoint.sh", "scripts/e2e_server.sh"])
    def test_timeout_outlives_the_wait_and_the_client_abort(self, path, settings):
        _, timeout = self._flags(path)
        assert timeout is not None
        assert int(timeout.group(1)) > settings.LIVE_LONGPOLL_WAIT + live_longpoll.ABORT_SLACK

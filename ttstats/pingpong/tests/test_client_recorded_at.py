"""KAN-89 -- ``PointEvent.client_recorded_at``, from the op's ``client_ts``.

A point synced after a dead spell reaches the server minutes after it was
played, so ``created_at`` (server time) no longer says when it happened. The
client's clock does, within reason: it is clamped to
``[match day - 1 day, now]`` and anything unparseable is stored as null.
``created_at`` stays the server time.
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.urls import reverse
from django.utils import timezone

from pingpong import live_ops
from pingpong import live_scoring as ls
from pingpong.models import Match, PointEvent

from .test_point_fidelity import _make_live_match


def _started_match(date_played=None):
    match, p1 = _make_live_match(best_of=5)
    fields = {"live_state": ls.set_initial_server(ls.initial_state(5), "team1")}
    if date_played is not None:
        fields["date_played"] = date_played
    Match.all_objects.filter(pk=match.pk).update(**fields)
    return Match.all_objects.get(pk=match.pk), p1


def _point(client, match, **extra):
    return client.post(
        reverse("pingpong:live_point", args=[match.pk]),
        data=json.dumps({"side": "team1", **extra}),
        content_type="application/json",
    )


def _sync_point(client, match, client_ts):
    return client.post(
        reverse("pingpong:live_sync", args=[match.pk]),
        data=json.dumps({"ops": [
            {"seq": 1, "type": "point", "side": "team1", "client_ts": client_ts},
        ]}),
        content_type="application/json",
    )


class TestParseClientTs:
    """The pure parse-and-clamp, no database."""

    DAY = datetime(2026, 11, 5, 18, 0, tzinfo=dt_timezone.utc)
    NOW = datetime(2026, 11, 5, 19, 30, tzinfo=dt_timezone.utc)

    def _parse(self, value):
        return live_ops.parse_client_ts(value, match_day=self.DAY.date(), now=self.NOW)

    def test_iso_with_z(self):
        assert self._parse("2026-11-05T19:03:11.120Z") == datetime(
            2026, 11, 5, 19, 3, 11, 120000, tzinfo=dt_timezone.utc
        )

    def test_offset_is_respected(self):
        assert self._parse("2026-11-05T20:03:11+01:00") == datetime(
            2026, 11, 5, 19, 3, 11, tzinfo=dt_timezone.utc
        )

    def test_naive_is_taken_as_utc(self):
        assert self._parse("2026-11-05T19:03:11") == datetime(
            2026, 11, 5, 19, 3, 11, tzinfo=dt_timezone.utc
        )

    def test_future_is_clamped_to_now(self):
        assert self._parse("2027-01-01T00:00:00Z") == self.NOW

    def test_too_far_past_is_clamped_to_the_day_before_the_match(self):
        assert self._parse("2020-01-01T00:00:00Z") == datetime(
            2026, 11, 4, 0, 0, tzinfo=dt_timezone.utc
        )

    def test_the_day_before_the_match_is_kept(self):
        assert self._parse("2026-11-04T23:59:00Z") == datetime(
            2026, 11, 4, 23, 59, tzinfo=dt_timezone.utc
        )

    @pytest.mark.parametrize(
        "garbage", [None, "", "yesterday", "2026-13-45T99:00:00Z", 1730000000, True, [], {}]
    )
    def test_garbage_is_none(self, garbage):
        assert self._parse(garbage) is None


@pytest.mark.django_db
class TestStoredOnThePoint:
    def test_single_op_point_stores_it(self, auth_client):
        match, p1 = _started_match()
        ts = timezone.now() - timedelta(minutes=3)
        resp = _point(auth_client(p1.user), match, client_ts=ts.isoformat())
        assert resp.status_code == 200
        event = PointEvent.objects.get(match=match)
        assert event.client_recorded_at == ts
        assert event.created_at > ts  # still the server's time

    def test_sync_stores_it(self, auth_client):
        match, p1 = _started_match()
        ts = timezone.now() - timedelta(minutes=10)
        assert _sync_point(auth_client(p1.user), match, ts.isoformat()).status_code == 200
        assert PointEvent.objects.get(match=match).client_recorded_at == ts

    def test_future_is_clamped(self, auth_client):
        match, p1 = _started_match()
        before = timezone.now()
        _sync_point(auth_client(p1.user), match, "2999-01-01T00:00:00Z")
        stored = PointEvent.objects.get(match=match).client_recorded_at
        assert before <= stored <= timezone.now()

    def test_past_is_clamped(self, auth_client):
        played = datetime(2026, 11, 5, 18, 0, tzinfo=dt_timezone.utc)
        match, p1 = _started_match(date_played=played)
        _sync_point(auth_client(p1.user), match, "2001-01-01T00:00:00Z")
        assert PointEvent.objects.get(match=match).client_recorded_at == datetime(
            2026, 11, 4, tzinfo=dt_timezone.utc
        )

    @pytest.mark.parametrize("garbage", ["not a time", 12345, None])
    def test_garbage_is_null_and_the_point_still_counts(self, auth_client, garbage):
        match, p1 = _started_match()
        resp = _sync_point(auth_client(p1.user), match, garbage)
        assert resp.status_code == 200
        assert resp.json()["state"]["team1_points"] == 1
        assert PointEvent.objects.get(match=match).client_recorded_at is None

    def test_absent_is_null(self, auth_client):
        match, p1 = _started_match()
        _point(auth_client(p1.user), match)
        assert PointEvent.objects.get(match=match).client_recorded_at is None

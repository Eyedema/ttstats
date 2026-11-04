"""Tests for the per-player daily notification limiter (KAN-38 / T3.7).

The limiter is a cache counter, so these need no database. `_clear_cache`
(autouse, conftest) gives each test an empty LocMemCache.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings

from pingpong import notification_limits as limits

NOON = datetime(2026, 11, 3, 12, 0, tzinfo=dt_timezone.utc)


class TestPerKindLimit:
    @override_settings(NOTIFICATION_DAILY_LIMITS={'season_ended': 1})
    def test_second_send_of_a_limited_kind_is_suppressed(self, caplog):
        caplog.set_level('INFO', logger='pingpong.notification_limits')
        assert limits.allow(7, 'season_ended', now=NOON) is True
        assert limits.allow(7, 'season_ended', now=NOON) is False
        assert 'suppressed' in caplog.text
        assert 'season_ended' in caplog.text

    @override_settings(NOTIFICATION_DAILY_LIMITS={'season_ended': 1})
    def test_limit_is_per_player(self):
        assert limits.allow(7, 'season_ended', now=NOON) is True
        assert limits.allow(8, 'season_ended', now=NOON) is True

    @override_settings(NOTIFICATION_DAILY_LIMITS={'season_ended': 1})
    def test_limit_resets_on_the_next_utc_day(self):
        assert limits.allow(7, 'season_ended', now=NOON) is True
        assert limits.allow(7, 'season_ended', now=NOON + timedelta(days=1)) is True

    @override_settings(NOTIFICATION_DAILY_LIMITS={}, NOTIFICATION_DAILY_CEILING=1000)
    def test_a_kind_without_a_limit_is_only_bound_by_the_ceiling(self):
        assert all(limits.allow(7, 'match_result', now=NOON) for _ in range(20))


class TestOverallCeiling:
    @override_settings(NOTIFICATION_DAILY_LIMITS={}, NOTIFICATION_DAILY_CEILING=3)
    def test_ceiling_applies_across_kinds(self, caplog):
        caplog.set_level('INFO', logger='pingpong.notification_limits')
        assert limits.allow(7, 'match_result', now=NOON)
        assert limits.allow(7, 'match_confirmation', now=NOON)
        assert limits.allow(7, 'scheduled_match', now=NOON)
        assert limits.allow(7, 'season_ended', now=NOON) is False
        assert 'ceiling' in caplog.text


class TestDefaults:
    def test_only_the_new_kind_has_a_per_kind_limit(self):
        # Existing kinds keep their behaviour: no per-kind limit, and a
        # ceiling far above a heavy club night's confirmation/result pushes.
        assert limits.DEFAULT_DAILY_LIMITS == {'season_ended': 1}
        assert limits.DEFAULT_DAILY_CEILING >= 50

    def test_defaults_apply_without_settings(self):
        assert limits.allow(7, 'season_ended', now=NOON) is True
        assert limits.allow(7, 'season_ended', now=NOON) is False


class TestFailClosed:
    def test_cache_error_on_add_suppresses(self, caplog):
        with patch.object(limits.cache, 'add', side_effect=ConnectionError('down')):
            assert limits.allow(7, 'match_result', now=NOON) is False
        assert 'fail' in caplog.text.lower()

    def test_cache_error_on_incr_suppresses(self):
        with patch.object(limits.cache, 'incr', side_effect=ConnectionError('down')):
            assert limits.allow(7, 'match_result', now=NOON) is False


class TestTtl:
    @pytest.mark.parametrize('now, expected', [
        (NOON, 12 * 3600),
        (datetime(2026, 11, 3, 23, 59, 30, tzinfo=dt_timezone.utc), 30),
        (datetime(2026, 11, 3, 0, 0, tzinfo=dt_timezone.utc), 24 * 3600),
    ])
    def test_seconds_until_utc_midnight(self, now, expected):
        assert limits.seconds_until_utc_midnight(now) == expected

    def test_counter_expires_at_the_end_of_the_utc_day(self):
        with patch.object(limits.cache, 'add', wraps=cache.add) as add:
            limits.allow(7, 'season_ended', now=NOON)

        timeouts = {call.args[2] for call in add.call_args_list}
        assert timeouts == {12 * 3600}

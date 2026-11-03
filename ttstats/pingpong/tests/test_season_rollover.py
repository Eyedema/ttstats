"""Season rollover (KAN-51, S4.6): freeze, archive, start the next, tell everyone.

The core guarantee is that a frozen table never changes: not on a late
confirmation, not on a re-run, not after a full `recalculate_elo`.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from pingpong import jobs, scheduled_jobs, season_rollover, seasons
from pingpong.models import (
    ClubMembership,
    JobRun,
    Match,
    PlayerRating,
    RatingChange,
    Season,
    SeasonStanding,
    Side,
)

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    SeasonFactory,
    SeasonStandingFactory,
    UserFactory,
    confirm_match,
)

Q1_START, Q1_END = date(2026, 1, 1), date(2026, 3, 31)
AFTER_GRACE = date(2026, 4, 10)


def _q1(club, **kwargs):
    return SeasonFactory(club=club, name="Q1 2026", starts_on=Q1_START, ends_on=Q1_END, **kwargs)


def _on(day, hour=12):
    return datetime(day.year, day.month, day.day, hour, tzinfo=dt_timezone.utc)


def _play(side1, side2, *, day, club, winner=Side.ONE):
    """A finished match on `day`. Unverified players auto-confirm, so Elo applies."""
    match = MatchFactory(
        team1_players=side1, team2_players=side2, is_double=len(side1) > 1,
        date_played=_on(day), club=club,
    )
    t1, t2 = (11, 4) if winner == Side.ONE else (4, 11)
    for n in (1, 2, 3):
        GameFactory(match=match, game_number=n, team1_score=t1, team2_score=t2)
    return match


def _verified_player(club, name=None):
    kwargs = {"name": name} if name else {}
    player = PlayerFactory(club=club, with_user=True, **kwargs)
    player.user.profile.email_verified = True
    player.user.profile.save()
    return player


def _table(season, fmt="singles"):
    return list(
        SeasonStanding.all_objects.filter(season=season, format=fmt)
        .order_by("rank", "-wins", "player_name")
        .values_list("player_name", "rank", "rating", "matches", "wins", "losses")
    )


def _snapshot():
    return sorted(
        SeasonStanding.all_objects.values_list(
            "season_id", "format", "player_id", "player_name",
            "rank", "rating", "matches", "wins", "losses",
        )
    )


def _run(today=AFTER_GRACE, dry_run=False):
    return season_rollover.run(today=today, dry_run=dry_run)


@pytest.fixture(autouse=True)
def _quiet_notifications():
    """No real pushes or mails unless a test asks; record who would hear."""
    with patch.object(season_rollover.notifications, "notify_season_ended", return_value=set()) as push, \
            patch.object(season_rollover.emails, "send_season_ended_email") as mail:
        yield push, mail


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSeasonStandingModel:
    def test_one_row_per_season_format_player(self):
        season = SeasonFactory()
        player = PlayerFactory()
        SeasonStandingFactory(season=season, player=player)
        with pytest.raises(IntegrityError), transaction.atomic():
            SeasonStandingFactory(season=season, player=player, rank=2)

    def test_a_deleted_player_keeps_their_frozen_row_and_name(self):
        club = ClubFactory()
        season = _q1(club)
        gone, other = PlayerFactory(club=club, name="Gone Gina"), PlayerFactory(club=club)
        _play([gone], [other], day=date(2026, 2, 1), club=club)
        _run()

        gone.delete()

        row = SeasonStanding.all_objects.get(season=season, player_name="Gone Gina")
        assert row.player_id is None
        assert row.rank == 1

    def test_a_renamed_player_keeps_the_name_they_finished_under(self):
        club = ClubFactory()
        season = _q1(club)
        p, q = PlayerFactory(club=club, name="Old Name"), PlayerFactory(club=club)
        _play([p], [q], day=date(2026, 2, 1), club=club)
        _run()

        p.name = "New Name"
        p.save()

        assert SeasonStanding.all_objects.get(season=season, player=p).player_name == "Old Name"


@pytest.fixture
def _clean_thread_locals():
    from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield _thread_locals
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.mark.django_db
class TestSeasonStandingManagerIsClubScoped:
    def _two_clubs(self):
        rows = []
        for _ in range(2):
            club = ClubFactory()
            _q1(club)
            _play([PlayerFactory(club=club)], [PlayerFactory(club=club)], day=date(2026, 2, 1), club=club)
            rows.append(club)
        _run()
        return rows

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_a_member_sees_only_their_clubs_standings(self, _clean_thread_locals):
        mine, theirs = self._two_clubs()
        _clean_thread_locals.user = UserFactory(is_staff=True)
        _clean_thread_locals.session = {}
        _clean_thread_locals.club = mine

        assert {s.season.club_id for s in SeasonStanding.objects.all()} == {mine.pk}

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_a_user_with_no_club_sees_nothing(self, _clean_thread_locals):
        self._two_clubs()
        _clean_thread_locals.user = UserFactory()
        _clean_thread_locals.session = {}

        assert not SeasonStanding.objects.exists()


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFreeze:
    def test_standings_are_frozen_from_the_seasons_ratings(self):
        club = ClubFactory()
        season = _q1(club)
        a, b, c = (PlayerFactory(club=club, name=n) for n in ("Ann", "Bob", "Cat"))
        _play([a], [b], day=date(2026, 1, 10), club=club)
        _play([a], [c], day=date(2026, 1, 11), club=club)
        _play([c], [b], day=date(2026, 1, 12), club=club)

        _run()

        ratings = {
            r.player_id: r for r in PlayerRating.all_objects.filter(season=season, format="singles")
        }
        table = _table(season)
        assert [row[0] for row in table] == ["Ann", "Cat", "Bob"]
        assert [row[1] for row in table] == [1, 2, 3]
        assert table[0][2:] == (ratings[a.pk].rating, 2, 2, 0)
        assert table[1][2:] == (ratings[c.pk].rating, 2, 1, 1)
        assert table[2][2:] == (ratings[b.pk].rating, 2, 0, 2)

    def test_both_formats_are_frozen_separately(self):
        club = ClubFactory()
        season = _q1(club)
        a, b, c, d = (PlayerFactory(club=club) for _ in range(4))
        _play([a], [b], day=date(2026, 1, 10), club=club)
        _play([a, b], [c, d], day=date(2026, 1, 11), club=club, winner=Side.TWO)

        _run()

        assert len(_table(season, "singles")) == 2
        doubles = SeasonStanding.all_objects.filter(season=season, format="doubles")
        assert {s.player_id: (s.wins, s.losses) for s in doubles} == {
            a.pk: (0, 1), b.pk: (0, 1), c.pk: (1, 0), d.pk: (1, 0),
        }
        assert set(doubles.values_list("rank", flat=True)) == {1, 3}

    def test_a_tie_for_first_shares_rank_one(self):
        club = ClubFactory()
        season = _q1(club)
        a, b, c, d = (PlayerFactory(club=club, name=n) for n in ("Ann", "Bob", "Cat", "Dan"))
        # Two separate pairs: both winners land on the same rating.
        _play([a], [b], day=date(2026, 1, 10), club=club)
        _play([c], [d], day=date(2026, 1, 10), club=club)

        _run()

        assert [(row[0], row[1]) for row in _table(season)] == [
            ("Ann", 1), ("Cat", 1), ("Bob", 3), ("Dan", 3),
        ]

    def test_only_this_seasons_matches_count(self):
        club = ClubFactory()
        season = _q1(club)
        a, b = PlayerFactory(club=club), PlayerFactory(club=club)
        _play([a], [b], day=date(2025, 12, 31), club=club)  # before the season
        _play([a], [b], day=date(2026, 2, 1), club=club)
        _play([b], [a], day=date(2026, 4, 1), club=club)  # in the next one

        _run()

        assert {(s.matches, s.wins + s.losses) for s in SeasonStanding.all_objects.filter(season=season)} == {(1, 1)}

    def test_the_season_is_archived(self):
        season = _q1(ClubFactory())
        _run()
        season.refresh_from_db()
        assert season.archived_at is not None

    def test_a_season_with_no_matches_archives_with_an_empty_table(self):
        club = ClubFactory()
        season = _q1(club)

        summary = _run()

        season.refresh_from_db()
        assert season.archived_at is not None
        assert not SeasonStanding.all_objects.filter(season=season).exists()
        assert Season.all_objects.filter(club=club).count() == 2
        assert "archived 1" in summary

    def test_nothing_happens_inside_the_grace_days(self):
        season = _q1(ClubFactory())
        _run(today=Q1_END + timedelta(days=3))
        season.refresh_from_db()
        assert season.archived_at is None

    def test_a_player_who_left_the_club_is_still_in_the_table(self):
        club = ClubFactory()
        season = _q1(club)
        leaver = PlayerFactory(club=club, with_user=True, name="Leaver")
        ClubMembershipFactory(club=club, user=leaver.user)
        stayer = PlayerFactory(club=club)
        _play([leaver], [stayer], day=date(2026, 2, 1), club=club)

        ClubMembership.objects.filter(user=leaver.user).delete()
        leaver.club = ClubFactory()
        leaver.save()
        _run()

        assert _table(season)[0][:2] == ("Leaver", 1)

    def test_a_club_created_mid_season_rolls_over_normally(self):
        club = ClubFactory()
        type(club).objects.filter(pk=club.pk).update(created_at=_on(date(2026, 2, 15)))
        season = _q1(club)
        a, b = PlayerFactory(club=club), PlayerFactory(club=club)
        _play([a], [b], day=date(2026, 3, 1), club=club)

        _run()

        assert len(_table(season)) == 2
        assert Season.all_objects.filter(club=club, starts_on=date(2026, 4, 1)).exists()

    def test_clubs_are_frozen_independently(self):
        club_a, club_b = ClubFactory(), ClubFactory()
        season_a, season_b = _q1(club_a), _q1(club_b)
        a1, a2 = PlayerFactory(club=club_a), PlayerFactory(club=club_a)
        b1, b2 = PlayerFactory(club=club_b), PlayerFactory(club=club_b)
        _play([a1], [a2], day=date(2026, 2, 1), club=club_a)
        _play([b1], [b2], day=date(2026, 2, 1), club=club_b)

        _run()

        assert set(SeasonStanding.all_objects.filter(season=season_a).values_list("player_id", flat=True)) == {a1.pk, a2.pk}
        assert set(SeasonStanding.all_objects.filter(season=season_b).values_list("player_id", flat=True)) == {b1.pk, b2.pk}

    def test_a_failure_rolls_back_the_whole_season(self):
        club = ClubFactory()
        season = _q1(club)
        _play([PlayerFactory(club=club)], [PlayerFactory(club=club)], day=date(2026, 2, 1), club=club)

        real_freeze = season_rollover._freeze

        def freeze_then_fail(s):
            assert real_freeze(s) > 0
            raise RuntimeError("boom")

        with patch.object(season_rollover, "_freeze", side_effect=freeze_then_fail):
            with pytest.raises(RuntimeError):
                _run()

        season.refresh_from_db()
        assert season.archived_at is None
        assert not SeasonStanding.all_objects.exists()

    def test_freezing_is_batched(self, monkeypatch):
        monkeypatch.setattr(season_rollover, "STANDINGS_BATCH_SIZE", 2)
        club = ClubFactory()
        season = _q1(club)
        players = [PlayerFactory(club=club) for _ in range(5)]
        for i in range(4):
            _play([players[i]], [players[i + 1]], day=date(2026, 1, 10 + i), club=club)

        _run()

        assert [row[1] for row in _table(season)] == sorted(row[1] for row in _table(season))
        assert len(_table(season)) == 5


# ---------------------------------------------------------------------------
# The next season
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNextSeason:
    def test_the_next_season_has_the_same_length_from_the_day_after(self):
        club = ClubFactory()
        _q1(club)
        _run()

        nxt = Season.all_objects.get(club=club, starts_on=date(2026, 4, 1))
        assert nxt.ends_on == date(2026, 6, 30)
        assert nxt.name == "Apr–Jun 2026"
        assert nxt.archived_at is None

    def test_not_created_when_a_later_season_already_exists(self):
        club = ClubFactory()
        _q1(club)
        SeasonFactory(club=club, starts_on=date(2026, 5, 1), ends_on=date(2026, 5, 31))

        _run()

        assert Season.all_objects.filter(club=club).count() == 2

    def test_a_missed_backlog_catches_up_in_one_run(self):
        club = ClubFactory()
        _q1(club)

        _run(today=date(2026, 10, 10))

        rows = list(Season.all_objects.filter(club=club).order_by("starts_on"))
        assert [(s.starts_on, s.archived_at is not None) for s in rows] == [
            (date(2026, 1, 1), True),
            (date(2026, 4, 1), True),
            (date(2026, 7, 1), True),
            (date(2026, 10, 1), False),
        ]


# ---------------------------------------------------------------------------
# The successor is created ahead of time, not at archive time
# ---------------------------------------------------------------------------

LEAD_DAY = Q1_END - timedelta(days=seasons.SUCCESSOR_LEAD_DAYS)


@pytest.mark.django_db
class TestSuccessorAheadOfTime:
    def test_created_on_the_lead_day_without_archiving(self):
        club = ClubFactory()
        season = _q1(club)

        summary = _run(today=LEAD_DAY)

        nxt = Season.all_objects.get(club=club, starts_on=date(2026, 4, 1))
        assert nxt.ends_on == date(2026, 6, 30)
        season.refresh_from_db()
        assert season.archived_at is None
        assert "created 1 next season" in summary

    def test_not_created_before_the_lead_day(self):
        club = ClubFactory()
        _q1(club)

        _run(today=LEAD_DAY - timedelta(days=1))

        assert Season.all_objects.filter(club=club).count() == 1

    def test_idempotent_across_runs(self):
        club = ClubFactory()
        _q1(club)

        for today in (LEAD_DAY, LEAD_DAY, Q1_END, AFTER_GRACE, AFTER_GRACE):
            _run(today=today)

        assert list(
            Season.all_objects.filter(club=club).order_by("starts_on").values_list("starts_on", "ends_on")
        ) == [(Q1_START, Q1_END), (date(2026, 4, 1), date(2026, 6, 30))]

    def test_never_overlaps_a_season_staff_already_created(self):
        club = ClubFactory()
        _q1(club)
        SeasonFactory(club=club, starts_on=date(2026, 4, 10), ends_on=date(2026, 5, 31))

        _run(today=LEAD_DAY)

        # The staff season is the latest; it is not yet in its own lead window.
        assert Season.all_objects.filter(club=club).count() == 2

    def test_only_the_latest_season_gets_a_successor(self):
        club = ClubFactory()
        SeasonFactory(club=club, starts_on=date(2025, 10, 1), ends_on=date(2025, 12, 31))
        _q1(club)

        _run(today=LEAD_DAY)

        assert sorted(Season.all_objects.filter(club=club).values_list("starts_on", flat=True)) == [
            date(2025, 10, 1), Q1_START, date(2026, 4, 1),
        ]

    def test_a_backlog_creates_every_missing_season_up_to_today(self):
        club = ClubFactory()
        _q1(club)

        _run(today=date(2026, 12, 20))  # inside Q4's lead window

        starts = list(Season.all_objects.filter(club=club).order_by("starts_on").values_list("starts_on", flat=True))
        assert starts == [Q1_START, date(2026, 4, 1), date(2026, 7, 1), date(2026, 10, 1), date(2027, 1, 1)]

    def test_dry_run_creates_nothing(self):
        club = ClubFactory()
        _q1(club)

        summary = _run(today=LEAD_DAY, dry_run=True)

        assert Season.all_objects.filter(club=club).count() == 1
        assert "create 1 next season" in summary

    def test_a_failure_in_one_club_does_not_stop_the_others(self):
        bad, good = ClubFactory(), ClubFactory()
        _q1(bad), _q1(good)
        real = season_rollover._create_successor

        def flaky(season_pk, *, today):
            if Season.all_objects.get(pk=season_pk).club_id == bad.pk:
                raise RuntimeError("boom")
            return real(season_pk, today=today)

        with patch.object(season_rollover, "_create_successor", side_effect=flaky):
            with pytest.raises(RuntimeError, match="failed"):
                _run(today=LEAD_DAY)

        assert Season.all_objects.filter(club=bad).count() == 1
        assert Season.all_objects.filter(club=good).count() == 2

    @override_settings(FEATURE_FLAGS={"seasons": True})
    def test_a_match_on_the_first_day_gets_season_ratings_and_shows_applied(self):
        """The KAN-9 review reproduction: a match confirmed on ends_on + 1."""
        club = ClubFactory()
        _q1(club)
        a, b = _verified_player(club, "Ann"), _verified_player(club, "Bob")
        _run(today=LEAD_DAY)

        match = _play([a], [b], day=Q1_END + timedelta(days=1), club=club)
        confirm_match(match)

        nxt = Season.all_objects.get(club=club, starts_on=Q1_END + timedelta(days=1))
        assert RatingChange.objects.filter(match=match, rating__season=nxt).count() == 2
        assert PlayerRating.all_objects.filter(season=nxt).count() == 2

        client = Client()
        client.force_login(a.user)
        ctx = client.get(reverse("pingpong:match_detail", args=[match.pk])).context
        assert ctx["rating_scope"].season_id == nxt.pk
        assert ctx["elo_is_pending"] is False
        assert ctx["side1_elo_delta"] == RatingChange.objects.get(
            match=match, player=a, rating__season=nxt
        ).rating_change


# ---------------------------------------------------------------------------
# Idempotency and dry run
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIdempotency:
    def test_running_twice_changes_nothing_the_second_time(self, _quiet_notifications):
        push, mail = _quiet_notifications
        club = ClubFactory()
        season = _q1(club)
        _play([PlayerFactory(club=club, with_user=True)], [PlayerFactory(club=club, with_user=True)],
              day=date(2026, 2, 1), club=club)
        _run()
        first = _snapshot()
        archived_at = Season.all_objects.get(pk=season.pk).archived_at
        sends = push.call_count

        summary = _run()

        assert _snapshot() == first
        assert Season.all_objects.get(pk=season.pk).archived_at == archived_at
        assert Season.all_objects.filter(club=club).count() == 2
        assert push.call_count == sends
        assert "archived 0" in summary

    def test_an_already_archived_season_is_skipped(self):
        season = _q1(ClubFactory(), archived_at=timezone.now())
        _run()
        assert not SeasonStanding.all_objects.filter(season=season).exists()


@pytest.mark.django_db
class TestDryRun:
    def test_reports_and_writes_nothing(self, _quiet_notifications):
        push, mail = _quiet_notifications
        club = ClubFactory()
        season = _q1(club)
        _play([PlayerFactory(club=club)], [PlayerFactory(club=club)], day=date(2026, 2, 1), club=club)

        summary = _run(dry_run=True)

        season.refresh_from_db()
        assert season.archived_at is None
        assert not SeasonStanding.all_objects.exists()
        assert Season.all_objects.filter(club=club).count() == 1
        assert push.call_count == 0
        assert "archive 1 season (" in summary
        assert "2 standings" in summary


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNotifications:
    def _season_with_verified_pair(self):
        club = ClubFactory()
        season = _q1(club)
        a, b = _verified_player(club, "Ann"), _verified_player(club, "Bob")
        match = _play([a], [b], day=date(2026, 2, 1), club=club)
        confirm_match(match)
        return season, a, b

    @override_settings(FEATURE_FLAGS={"seasons": True})
    def test_each_ranked_player_is_told_once_with_every_format(self, _quiet_notifications):
        push, mail = _quiet_notifications
        club = ClubFactory()
        season = _q1(club)
        a, b, c, d = (PlayerFactory(club=club, with_user=True) for _ in range(4))
        _play([a], [b], day=date(2026, 1, 10), club=club)
        _play([a, c], [b, d], day=date(2026, 1, 11), club=club)

        _run()

        told = {call.args[0].pk: call.kwargs for call in push.call_args_list}
        assert set(told) == {a.pk, b.pk, c.pk, d.pk}
        assert push.call_count == 4
        assert told[a.pk]["season_name"] == "Q1 2026"
        assert told[a.pk]["standings"] == [("Singles", 1), ("Doubles", 1)]
        assert told[c.pk]["standings"] == [("Doubles", 1)]
        assert told[a.pk]["url"] == f"/pingpong/seasons/{season.pk}/"

    @override_settings(FEATURE_FLAGS={"seasons": True})
    def test_push_preferred_and_email_for_whoever_push_missed(self, _quiet_notifications):
        push, mail = _quiet_notifications
        season, a, b = self._season_with_verified_pair()
        push.side_effect = lambda player, **kw: {player.pk} if player.pk == a.pk else set()

        _run()

        assert [call.args[0].pk for call in mail.call_args_list] == [b.pk]

    @override_settings(FEATURE_FLAGS={"seasons": True})
    def test_unverified_addresses_are_not_emailed(self, _quiet_notifications):
        push, mail = _quiet_notifications
        club = ClubFactory()
        _q1(club)
        _play([PlayerFactory(club=club, with_user=True)], [PlayerFactory(club=club, with_user=True)],
              day=date(2026, 2, 1), club=club)

        _run()

        assert push.call_count == 2
        assert mail.call_count == 0

    @override_settings(FEATURE_FLAGS={"seasons": True})
    def test_a_crash_after_archiving_still_notifies_on_the_next_run(self, _quiet_notifications):
        push, mail = _quiet_notifications
        season, a, b = self._season_with_verified_pair()
        with patch.object(season_rollover, "notify_pending", side_effect=RuntimeError("down")):
            with pytest.raises(RuntimeError):
                _run()
        assert Season.all_objects.get(pk=season.pk).archived_at is not None
        assert push.call_count == 0

        _run()
        _run()

        assert sorted(call.args[0].pk for call in push.call_args_list) == sorted([a.pk, b.pk])

    @override_settings(FEATURE_FLAGS={"seasons": True})
    def test_only_unclaimed_players_are_told_again(self, _quiet_notifications):
        push, mail = _quiet_notifications
        season, a, b = self._season_with_verified_pair()
        _run()
        SeasonStanding.all_objects.filter(player=a).update(notified_at=None)
        # b is still claimed; only a (unclaimed by hand) is pending.
        assert season_rollover.notify_pending() == 1
        assert [call.args[0].pk for call in push.call_args_list].count(a.pk) == 2
        assert [call.args[0].pk for call in push.call_args_list].count(b.pk) == 1

    def test_with_the_seasons_flag_off_nobody_is_told(self, _quiet_notifications):
        push, mail = _quiet_notifications
        season, a, b = self._season_with_verified_pair()

        _run()

        assert push.call_count == 0 and mail.call_count == 0
        # ...and turning the flag on later does not dig up an old season.
        with override_settings(FEATURE_FLAGS={"seasons": True}):
            _run()
        assert push.call_count == 0

    @override_settings(FEATURE_FLAGS={"seasons": True})
    def test_a_player_without_an_account_is_skipped(self, _quiet_notifications):
        push, mail = _quiet_notifications
        club = ClubFactory()
        _q1(club)
        _play([PlayerFactory(club=club)], [PlayerFactory(club=club)], day=date(2026, 2, 1), club=club)

        _run()

        assert push.call_count == 0
        assert not SeasonStanding.all_objects.filter(notified_at__isnull=True).exists()


# ---------------------------------------------------------------------------
# The late-confirmation rule and the core guarantee
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFrozenTablesNeverChange:
    def test_a_late_confirmation_updates_ratings_but_not_the_table(self):
        club = ClubFactory()
        season = _q1(club)
        a, b = _verified_player(club, "Ann"), _verified_player(club, "Bob")
        early = _play([a], [b], day=date(2026, 3, 1), club=club)
        confirm_match(early)
        late = _play([b], [a], day=date(2026, 3, 31), club=club, winner=Side.ONE)
        assert not Match.all_objects.get(pk=late.pk).is_confirmed

        _run()
        frozen = _table(season)
        rating_before = PlayerRating.all_objects.get(player=b, season=season).rating

        confirm_match(late)

        assert PlayerRating.all_objects.get(player=b, season=season).rating > rating_before
        assert _table(season) == frozen

    def test_a_live_match_across_the_boundary_does_not_alter_the_table(self):
        club = ClubFactory()
        season = _q1(club)
        a, b = PlayerFactory(club=club), PlayerFactory(club=club)
        _play([a], [b], day=date(2026, 3, 1), club=club)
        live = MatchFactory(player1=b, player2=a, club=club, is_live=True, date_played=_on(Q1_END, 23))

        _run()
        frozen = _table(season)

        Match.all_objects.filter(pk=live.pk).update(is_live=False)
        live.is_live = False
        for n in (1, 2, 3):
            GameFactory(match=live, game_number=n, team1_score=11, team2_score=3)

        assert PlayerRating.all_objects.get(player=b, season=season).matches == 2
        assert _table(season) == frozen

    def test_an_archived_table_survives_a_full_recalculate_elo(self):
        club = ClubFactory()
        _q1(club)
        a, b, c = (_verified_player(club) for _ in range(3))
        for day, (w, l) in enumerate([(a, b), (b, c), (c, a), (a, c)], start=1):
            confirm_match(_play([w], [l], day=date(2026, 3, day), club=club))
        late = _play([c], [b], day=date(2026, 3, 30), club=club)
        _run()
        frozen = _snapshot()
        confirm_match(late)

        call_command("recalculate_elo", stdout=StringIO())

        assert _snapshot() == frozen


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestJob:
    def test_registered_as_season_rollover(self):
        assert "season_rollover" in jobs.job_names()

    def test_the_job_runs_rollover_and_logs_its_summary(self):
        club = ClubFactory()
        _q1(club)
        with patch.object(season_rollover.timezone, "now", return_value=_on(AFTER_GRACE)):
            jobs.run_job("season_rollover")

        run = JobRun.objects.get(name="season_rollover")
        assert run.status == JobRun.Status.SUCCESS
        assert "archived 1" in run.summary

    def test_the_job_dry_run_writes_nothing(self):
        season = _q1(ClubFactory())
        with patch.object(season_rollover.timezone, "now", return_value=_on(AFTER_GRACE)):
            summary = scheduled_jobs.season_rollover(dry_run=True)

        season.refresh_from_db()
        assert season.archived_at is None
        assert "archive 1 season (" in summary

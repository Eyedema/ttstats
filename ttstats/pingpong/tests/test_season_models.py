"""The Season model (KAN-46 / S4.1): club-scoped, non-overlapping in the database."""
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, override_settings
from django.utils import timezone

from .. import seasons
from ..admin import SeasonAdmin
from ..models import Season
from .conftest import ClubFactory, SeasonFactory, UserFactory


def _rejected(**kwargs):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Season.all_objects.create(**kwargs)


@pytest.mark.django_db
class TestSeasonCreation:
    def test_factory_builds_a_valid_season_in_its_own_club(self):
        season = SeasonFactory()

        assert season.club_id is not None
        assert season.starts_on <= season.ends_on
        assert season.archived_at is None
        assert season.name in str(season)

    def test_factory_seasons_in_one_club_do_not_overlap(self):
        club = ClubFactory()
        a, b = SeasonFactory(club=club), SeasonFactory(club=club)

        assert not seasons.overlaps(a.starts_on, a.ends_on, b.starts_on, b.ends_on)

    def test_ends_before_it_starts_is_rejected_by_the_database(self):
        _rejected(club=ClubFactory(), name="Backwards",
                  starts_on=date(2026, 2, 1), ends_on=date(2026, 1, 31))

    def test_a_one_day_season_is_allowed(self):
        Season.all_objects.create(club=ClubFactory(), name="Day",
                                  starts_on=date(2026, 2, 1), ends_on=date(2026, 2, 1))


@pytest.mark.django_db
class TestNonOverlapIsEnforcedByTheDatabase:
    def _q1(self, club):
        return Season.all_objects.create(club=club, name="Q1",
                                         starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))

    def test_an_overlapping_insert_in_the_same_club_is_rejected(self):
        club = ClubFactory()
        self._q1(club)

        _rejected(club=club, name="Overlap", starts_on=date(2026, 3, 31), ends_on=date(2026, 5, 1))

    def test_a_season_inside_another_is_rejected(self):
        club = ClubFactory()
        self._q1(club)

        _rejected(club=club, name="Inside", starts_on=date(2026, 2, 1), ends_on=date(2026, 2, 2))

    def test_adjacent_seasons_are_allowed(self):
        club = ClubFactory()
        self._q1(club)

        Season.all_objects.create(club=club, name="Q2",
                                  starts_on=date(2026, 4, 1), ends_on=date(2026, 6, 30))

    def test_the_same_dates_in_another_club_are_allowed(self):
        self._q1(ClubFactory())
        self._q1(ClubFactory())

        assert Season.all_objects.count() == 2

    def test_an_update_that_creates_an_overlap_is_rejected(self):
        club = ClubFactory()
        self._q1(club)
        q2 = Season.all_objects.create(club=club, name="Q2",
                                       starts_on=date(2026, 4, 1), ends_on=date(2026, 6, 30))

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Season.all_objects.filter(pk=q2.pk).update(starts_on=date(2026, 3, 15))

    def test_updating_a_season_without_touching_its_dates_is_allowed(self):
        season = self._q1(ClubFactory())
        season.name = "Winter"
        season.save()

        assert Season.all_objects.get(pk=season.pk).name == "Winter"


@pytest.mark.django_db
class TestSeasonClean:
    def test_clean_reports_an_overlap_as_a_validation_error(self):
        club = ClubFactory()
        SeasonFactory(club=club, starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))
        clash = Season(club=club, name="Clash", starts_on=date(2026, 3, 1), ends_on=date(2026, 4, 30))

        with pytest.raises(ValidationError):
            clash.full_clean()

    def test_clean_rejects_ends_before_starts(self):
        season = Season(club=ClubFactory(), name="x",
                        starts_on=date(2026, 2, 1), ends_on=date(2026, 1, 1))

        with pytest.raises(ValidationError):
            season.full_clean()

    def test_archived_dates_cannot_be_edited(self):
        season = SeasonFactory(archived_at=timezone.now())
        season.ends_on = season.ends_on + timedelta(days=1)

        with pytest.raises(ValidationError):
            season.full_clean()

    def test_archived_season_name_can_still_be_edited(self):
        season = SeasonFactory(archived_at=timezone.now())
        season.name = "Renamed"

        season.full_clean()


@pytest.mark.django_db
class TestSeasonResolutionAgainstRows:
    def test_for_day_resolves_within_one_club(self):
        club, other = ClubFactory(), ClubFactory()
        q1 = SeasonFactory(club=club, starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))
        SeasonFactory(club=other, starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))

        assert Season.for_day(club.pk, date(2026, 3, 31)) == q1
        assert Season.for_day(club.pk, date(2026, 4, 1)) is None

    def test_for_day_uses_the_utc_date_of_a_datetime(self):
        club = ClubFactory()
        q1 = SeasonFactory(club=club, starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))
        last_instant = datetime(2026, 3, 31, 23, 59, 59, tzinfo=dt_timezone.utc)

        assert Season.for_day(club.pk, last_instant) == q1
        assert Season.for_day(club.pk, last_instant + timedelta(seconds=1)) is None

    def test_a_match_without_a_club_has_no_season(self):
        assert Season.for_day(None, date(2026, 1, 1)) is None


@pytest.mark.django_db
class TestSeasonAdmin:
    def _admin(self):
        return SeasonAdmin(Season, AdminSite())

    def _request(self):
        request = RequestFactory().get("/")
        request.user = UserFactory(is_staff=True, is_superuser=True)
        return request

    def test_dates_are_editable_while_open(self):
        fields = self._admin().get_readonly_fields(self._request(), SeasonFactory())

        assert "starts_on" not in fields and "ends_on" not in fields

    def test_dates_are_read_only_once_archived(self):
        season = SeasonFactory(archived_at=timezone.now())

        fields = self._admin().get_readonly_fields(self._request(), season)

        assert {"starts_on", "ends_on", "club"} <= set(fields)


@pytest.mark.django_db
class TestSeasonsFlag:
    def test_declared_and_off_by_default(self):
        from django.conf import settings

        from .. import flags

        assert "seasons" in settings.FEATURE_FLAGS
        assert flags.is_enabled("seasons") is False


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
class TestSeasonManagerIsClubScoped:
    """Seasons are club data: nothing bypasses the club filter."""

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_a_member_sees_only_their_clubs_seasons(self, _clean_thread_locals):
        mine, theirs = SeasonFactory(), SeasonFactory()
        user = UserFactory(is_staff=True)
        _clean_thread_locals.user = user
        _clean_thread_locals.session = {}
        _clean_thread_locals.club = mine.club

        assert list(Season.objects.all()) == [mine]
        assert theirs not in Season.objects.all()

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_a_user_with_no_club_sees_nothing(self, _clean_thread_locals):
        SeasonFactory()
        _clean_thread_locals.user = UserFactory()
        _clean_thread_locals.session = {}

        assert not Season.objects.exists()

    def test_no_viewer_sees_everything(self):
        SeasonFactory(), SeasonFactory()

        assert Season.objects.count() == 2

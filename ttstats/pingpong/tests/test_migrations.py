"""Migrations must be two-way.

`0028_drop_team` is the counter-example the epic keeps pointing at: it raises
`RuntimeError` on reverse, so its only remedy is restoring a snapshot. The
tenancy migrations must not be that, because the rollback plan in T1.12 depends
on reversing them.

These tests drive the real `MigrationExecutor` rather than inspecting the
operation list, because "AddField is reversible in principle" is not the claim
being made -- the claim is that *this chain* unapplies and reapplies against a
database with rows in it.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

APP = "pingpong"
HEAD = "0032_club_models"
BEFORE_CLUBS = "0031_feature_flag_overrides"
BEFORE_FLAGS = "0030_match_result_notified"


def _migrate_to(target):
    """Run the executor to `target`, rebuilding the graph each time.

    The loader caches the applied-migration set, so a second migrate on a stale
    executor is a no-op that looks like success.
    """
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)])


def _columns(table):
    with connection.cursor() as cursor:
        return {
            col.name
            for col in connection.introspection.get_table_description(cursor, table)
        }


def _tables():
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


@pytest.fixture
def at_head():
    """Leave the database at migration head however the test ends.

    Without this a failure mid-reverse would hand every later test in the
    session a schema with no club columns.
    """
    yield
    _migrate_to(HEAD)


@pytest.mark.django_db(transaction=True)
class TestTenancyMigrationsAreTwoWay:
    def test_head_has_the_club_tables_and_columns(self):
        tables = _tables()
        assert "pingpong_club" in tables
        assert "pingpong_clubmembership" in tables
        for table in (
            "pingpong_player",
            "pingpong_match",
            "pingpong_scheduledmatch",
            "pingpong_championship",
            "pingpong_location",
        ):
            assert "club_id" in _columns(table), table

    def test_0032_reverses_and_reapplies(self, at_head):
        _migrate_to(BEFORE_CLUBS)

        assert "pingpong_club" not in _tables()
        assert "club_id" not in _columns("pingpong_player")
        assert "club_id" not in _columns("pingpong_match")

        _migrate_to(HEAD)

        assert "pingpong_club" in _tables()
        assert "club_id" in _columns("pingpong_player")

    def test_0031_reverses_and_reapplies(self, at_head):
        _migrate_to(BEFORE_FLAGS)
        assert "pingpong_featureflagoverride" not in _tables()

        _migrate_to(HEAD)
        assert "pingpong_featureflagoverride" in _tables()

    def test_the_whole_tenancy_chain_reverses_with_data_present(self, at_head):
        """The rollback in T1.12 happens against a populated database, not an
        empty one. Reversing must not need the club columns to be empty."""
        from pingpong.models import Club, Player

        club = Club.objects.create(name="Rollback Rehearsal")
        Player.objects.create(name="Ada", club=club)

        _migrate_to(BEFORE_FLAGS)

        # This is the whole point of the rollback path: the club *scoping*
        # disappears, the app's actual data does not. A player keeps existing
        # with the column removed from under them.
        assert "pingpong_club" not in _tables()
        assert "club_id" not in _columns("pingpong_player")
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM pingpong_player")
            assert cursor.fetchone()[0] == 1

        _migrate_to(HEAD)
        assert Player.objects.count() == 1
        assert Player.objects.get().club_id is None

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
HEAD = "0035_backfill_clubs"
BEFORE_BACKFILL = "0034_club_invites"
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

    def test_the_club_columns_reverse_and_reapply(self, at_head):
        _migrate_to(BEFORE_CLUBS)

        assert "pingpong_club" not in _tables()
        assert "club_id" not in _columns("pingpong_player")
        assert "club_id" not in _columns("pingpong_match")

        _migrate_to(HEAD)

        assert "pingpong_club" in _tables()
        assert "club_id" in _columns("pingpong_player")

    def test_the_flag_table_reverses_and_reapplies(self, at_head):
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
        # Re-forwarding runs the backfill again, so the player comes back with
        # the default club rather than a null -- which is the whole point of
        # rolling forward after a rollback.
        assert Player.objects.count() == 1
        assert Player.objects.get().club.slug == "ttstats"


@pytest.mark.django_db(transaction=True)
class TestBackfillMigrationIsTwoWay:
    """`0028_drop_team` raises RuntimeError on reverse and its documented
    remedy is "restore a snapshot". The T1.12 rollback plan reverses this one
    as a routine step, so it has to actually work -- against rows, not an
    empty table."""

    def test_the_backfill_assigns_forward_and_clears_on_reverse(self, at_head):
        from pingpong.models import Club, Match, Player
        from pingpong.services import set_match_sides

        # Start from the schema-only state, as production will be after step 2
        # of the runbook and before step 3.
        _migrate_to(BEFORE_BACKFILL)

        p1 = Player.objects.create(name="Ada")
        p2 = Player.objects.create(name="Bob")
        match = Match.all_objects.create(best_of=5)
        set_match_sides(match, [p1], [p2])
        before = (Player.objects.count(), Match.all_objects.count())

        _migrate_to(HEAD)
        assert Player.objects.filter(club__isnull=True).count() == 0
        assert Match.all_objects.filter(club__isnull=True).count() == 0
        assert (Player.objects.count(), Match.all_objects.count()) == before

        _migrate_to(BEFORE_BACKFILL)
        assert Player.objects.filter(club__isnull=True).count() == Player.objects.count()
        assert not Club.objects.filter(slug="ttstats").exists()
        assert (Player.objects.count(), Match.all_objects.count()) == before

    def test_reversing_the_backfill_does_not_trip_the_protect_constraints(self, at_head):
        """Every club FK is PROTECT, so the reverse has to null them all before
        it can delete the club. Getting the order wrong is a ProtectedError
        mid-rollback, which is the worst possible moment for one."""
        from pingpong.models import Club, Location, Player

        _migrate_to(BEFORE_BACKFILL)
        Player.objects.create(name="Ada")
        Location.objects.create(name="Court 1")

        _migrate_to(HEAD)
        _migrate_to(BEFORE_BACKFILL)

        assert not Club.objects.exists()


@pytest.mark.django_db(transaction=True)
class TestTheChainSurvivesADatabaseWithUsersInIt:
    """The gap that let a broken migration reach a real database.

    `0035_backfill_clubs` used to be `0033`, ahead of the migration that adds
    `ClubMembership.role` -- and it writes that field. Migrations run against
    *historical* models, so the column did not exist yet and the run died with
    `TypeError: ClubMembership() got unexpected keyword arguments: 'role'`.

    The whole suite was green, because the failing line only runs when there
    are **Users** to create memberships for, and the test database has none at
    migration time. Every other test here created Players but no Users.

    So the fixture below is the point of this class: migrate forward against a
    database shaped like a real one -- users, players, matches, participants --
    rather than an empty schema. That is the difference between a migration
    test and a migration test that means something.
    """

    def _populate(self):
        """A pre-tenancy world, built at the pre-backfill state.

        Populated at BEFORE_BACKFILL rather than BEFORE_CLUBS because the ORM
        used here is the *current* one and knows about `club_id`. That is also
        the shape production is in after runbook step 2: columns present,
        every one of them null.
        """
        from django.contrib.auth.models import User

        from pingpong.models import Location, Match, Player
        from pingpong.services import set_match_sides

        location = Location.objects.create(name="Court 1")
        players = []
        for i in range(3):
            user = User.objects.create_user(f"chainuser{i}", password="x")
            players.append(Player.objects.create(name=f"Chain {i}", user=user))
        players.append(Player.objects.create(name="Chain guest"))

        for i in range(0, 4, 2):
            match = Match.all_objects.create(best_of=5, location=location)
            set_match_sides(match, [players[i]], [players[i + 1]])

        return User.objects.count(), Player.objects.count(), Match.all_objects.count()

    def test_migrating_forward_with_users_present(self, at_head):
        from django.contrib.auth.models import User

        from pingpong.models import ClubMembership, ClubRole, Match, Player

        _migrate_to(BEFORE_BACKFILL)
        users, players, matches = self._populate()

        _migrate_to(HEAD)

        # Nothing lost.
        assert User.objects.count() == users
        assert Player.objects.count() == players
        assert Match.all_objects.count() == matches

        # Everything assigned.
        assert Player.objects.filter(club__isnull=True).count() == 0
        assert Match.all_objects.filter(club__isnull=True).count() == 0

        # Every user in a club -- without this the app is empty for everyone.
        assert ClubMembership.objects.count() == users

        # And with the capability their role is supposed to preserve. This is
        # the assertion the ordering bug would have broken silently even if the
        # TypeError had not fired: memberships created before 0033 would have
        # taken the model default (`member`) and quietly demoted everyone.
        roles = set(ClubMembership.objects.values_list("role", flat=True))
        assert roles <= {ClubRole.OWNER, ClubRole.ADMIN}
        assert ClubMembership.objects.filter(role=ClubRole.OWNER).count() == 1

    def test_the_whole_chain_reverses_with_users_present(self, at_head):
        from django.contrib.auth.models import User

        from pingpong.models import Player

        _migrate_to(BEFORE_BACKFILL)
        users, players, matches = self._populate()

        _migrate_to(HEAD)
        _migrate_to(BEFORE_CLUBS)

        assert User.objects.count() == users
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM pingpong_player")
            assert cursor.fetchone()[0] == players
        assert "club_id" not in _columns("pingpong_player")

        _migrate_to(HEAD)
        assert Player.objects.filter(club__isnull=True).count() == 0

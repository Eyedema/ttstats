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


def _create_player(at, **fields):
    """A Player created through the *historical* model at migration `at`.

    The current model no longer declares columns that older schemas have as
    NOT NULL without a database default (KAN-73 dropped `Player.elo_rating`
    and friends), so it cannot insert at an old state. Returns the current
    model's instance, which reads fine: its columns are a subset.
    """
    from pingpong.models import Player

    executor = MigrationExecutor(connection)
    old_player = executor.loader.project_state((APP, at)).apps.get_model(APP, "Player")
    return Player._base_manager.get(pk=old_player._base_manager.create(**fields).pk)


def _create_match(at, **fields):
    """A Match inserted through the *historical* model at migration `at`.

    The mirror image of `_create_player`: the current model declares columns
    that older schemas do not have yet (KAN-77 added `live_applied_seq`), so
    it can neither insert nor load a row there. Returns an unsaved-shell
    current-model instance carrying only the pk, which is all
    `set_match_sides` needs.
    """
    from pingpong.models import Match

    executor = MigrationExecutor(connection)
    old_match = executor.loader.project_state((APP, at)).apps.get_model(APP, "Match")
    return Match(pk=old_match._base_manager.create(**fields).pk)


def _leaf():
    """The app's real latest migration, not the tenancy HEAD constant."""
    executor = MigrationExecutor(connection)
    (leaf,) = [name for app, name in executor.loader.graph.leaf_nodes() if app == APP]
    return leaf


@pytest.fixture
def at_head():
    """Leave the database at the latest migration however the test ends.

    Without this a failure mid-reverse would hand every later test in the
    session a schema with no club columns -- or, since KAN-9, no seasons, and
    the Elo write path needs those on every confirmation.
    """
    yield
    _migrate_to(_leaf())


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

        p1 = _create_player(BEFORE_BACKFILL, name="Ada")
        p2 = _create_player(BEFORE_BACKFILL, name="Bob")
        match = _create_match(BEFORE_BACKFILL, best_of=5)
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
        from pingpong.models import Club, Location

        _migrate_to(BEFORE_BACKFILL)
        _create_player(BEFORE_BACKFILL, name="Ada")
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
        from django.db.models.signals import post_save

        from pingpong.models import Location, Match, Player
        from pingpong.services import set_match_sides
        from pingpong.signals import create_user_profile

        location = Location.objects.create(name="Court 1")
        players = []
        # The profile/preference signal writes through the current models, so
        # any column added to them after BEFORE_BACKFILL (e.g. a new
        # NotificationPreference toggle) would not exist yet. The backfill
        # only needs Users, so the signal is muted while they are created.
        post_save.disconnect(create_user_profile, sender=User)
        try:
            for i in range(3):
                user = User.objects.create_user(f"chainuser{i}", password="x")
                players.append(_create_player(BEFORE_BACKFILL, name=f"Chain {i}", user_id=user.pk))
        finally:
            post_save.connect(create_user_profile, sender=User)
        players.append(_create_player(BEFORE_BACKFILL, name="Chain guest"))

        for i in range(0, 4, 2):
            match = _create_match(BEFORE_BACKFILL, best_of=5, location_id=location.pk)
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


def _sqlite_triggers():
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        return {row[0] for row in cursor.fetchall()}


BEFORE_SEASONS = "0037_point_summaries"
SEASONS = "0038_season"


@pytest.mark.django_db(transaction=True)
class TestSeasonMigrationIsTwoWay:
    """KAN-46: the season table and its non-overlap guard reverse cleanly."""

    def test_the_overlap_triggers_exist_at_head(self):
        """SQLite table rebuilds drop triggers silently; this catches a later
        migration that alters pingpong_season without recreating them."""
        assert {"season_no_overlap_insert", "season_no_overlap_update"} <= _sqlite_triggers()

    def test_reverses_and_reapplies_with_a_season_present(self, at_head):
        from datetime import date

        from pingpong.models import Club, Season

        _migrate_to(SEASONS)
        club = Club.objects.create(name="Reverse Rehearsal")
        Season.all_objects.create(club=club, name="Q1",
                                  starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))

        _migrate_to(BEFORE_SEASONS)
        assert "pingpong_season" not in _tables()
        assert not {"season_no_overlap_insert", "season_no_overlap_update"} & _sqlite_triggers()

        _migrate_to(_leaf())
        assert "pingpong_season" in _tables()
        assert {"season_no_overlap_insert", "season_no_overlap_update"} <= _sqlite_triggers()


BEFORE_RETIRE_ELO = "0045_ratingchange_glicko_history"
RETIRE_ELO = "0046_retire_elo"


@pytest.mark.django_db(transaction=True)
class TestRetireEloMigration:
    """KAN-73: the derived Elo columns go, the Elo audit trail is renamed."""

    def test_head_has_no_derived_elo_columns(self):
        assert not {"elo_rating", "elo_peak", "matches_for_elo"} & _columns("pingpong_player")
        assert not {"rating", "peak"} & _columns("pingpong_playerrating")
        rc = _columns("pingpong_ratingchange")
        assert {"elo_old_rating", "elo_new_rating", "elo_change", "elo_k_factor"} <= rc
        assert not {"old_rating", "new_rating", "rating_change", "k_factor"} & rc
        assert "pingpong_elohistory" in _tables()  # frozen, kept

    def test_reverses_and_reapplies_on_an_empty_rating_table(self, at_head):
        _migrate_to(BEFORE_RETIRE_ELO)
        assert {"elo_rating", "elo_peak", "matches_for_elo"} <= _columns("pingpong_player")
        assert {"old_rating", "new_rating", "rating_change", "k_factor"} <= _columns(
            "pingpong_ratingchange"
        )

        _migrate_to(RETIRE_ELO)
        assert "elo_rating" not in _columns("pingpong_player")
        assert "elo_old_rating" in _columns("pingpong_ratingchange")


LIVE_APPLIED_SEQ = "0047_match_live_applied_seq"


@pytest.mark.django_db(transaction=True)
class TestLiveAppliedSeqMigration:
    """KAN-85: a nullable column, so it reverses and re-applies freely."""

    def test_reverses_and_reapplies(self, at_head):
        assert "live_applied_seq" in _columns("pingpong_match")

        _migrate_to(RETIRE_ELO)
        assert "live_applied_seq" not in _columns("pingpong_match")

        _migrate_to(LIVE_APPLIED_SEQ)
        assert "live_applied_seq" in _columns("pingpong_match")


CLIENT_RECORDED_AT = "0048_pointevent_client_recorded_at"


@pytest.mark.django_db(transaction=True)
class TestClientRecordedAtMigration:
    """KAN-89: a nullable column, so it reverses and re-applies freely."""

    def test_reverses_and_reapplies(self, at_head):
        assert "client_recorded_at" in _columns("pingpong_pointevent")

        _migrate_to(LIVE_APPLIED_SEQ)
        assert "client_recorded_at" not in _columns("pingpong_pointevent")

        _migrate_to(CLIENT_RECORDED_AT)
        assert "client_recorded_at" in _columns("pingpong_pointevent")


LIVE_SCORER = "0049_live_scorer"


@pytest.mark.django_db(transaction=True)
class TestLiveScorerMigration:
    """KAN-158: the table, ``Match.live_rev``, and host rows for the matches
    that were live at deploy time -- forward, backward and forward again."""

    def _old(self):
        return MigrationExecutor(connection).loader.project_state(
            (APP, CLIENT_RECORDED_AT)
        ).apps

    def _world(self):
        """At 0048: one live match (scorekeeper on side 2, seq 4), one live
        match whose scorekeeper is not a participant (staff umpire), and one
        finished live-scored match."""
        apps = self._old()
        Player = apps.get_model(APP, "Player")
        Match = apps.get_model(APP, "Match")
        Participant = apps.get_model(APP, "MatchParticipant")
        p1 = Player._base_manager.create(name="Mig One")
        p2 = Player._base_manager.create(name="Mig Two")
        umpire = Player._base_manager.create(name="Mig Umpire")

        def match(**fields):
            m = Match._base_manager.create(best_of=5, **fields)
            Participant.objects.create(match=m, player=p1, side=1)
            Participant.objects.create(match=m, player=p2, side=2)
            return m.pk

        live = match(is_live=True, scorekeeper=p2, live_applied_seq=4, live_state={"x": 1})
        umpired = match(is_live=True, scorekeeper=umpire, live_state={"x": 1})
        finished = match(is_live=False, scorekeeper=p1, live_applied_seq=9)
        return live, umpired, finished, p2.pk, umpire.pk

    def test_forward_backward_forward(self, at_head):
        _migrate_to(CLIENT_RECORDED_AT)
        live, umpired, finished, p2, umpire = self._world()

        _migrate_to(LIVE_SCORER)
        from pingpong.models import LiveScorer, Match

        assert "live_rev" in _columns("pingpong_match")
        row = LiveScorer.objects.get(match_id=live)
        assert (row.player_id, row.side, row.role) == (p2, 2, "host")
        assert row.applied_seq == 4
        assert row.can_score_both is True
        assert row.revoked_at is None
        umpire_row = LiveScorer.objects.get(match_id=umpired)
        assert (umpire_row.player_id, umpire_row.side, umpire_row.applied_seq) == (umpire, 1, 0)
        # Only matches live at deploy time: the finished one stays row-less.
        assert not LiveScorer.objects.filter(match_id=finished).exists()

        # The host's stream advances after the deploy, past the mirror.
        LiveScorer.objects.filter(pk=row.pk).update(applied_seq=7)

        _migrate_to(CLIENT_RECORDED_AT)
        assert "pingpong_livescorer" not in _tables()
        assert "live_rev" not in _columns("pingpong_match")
        old_match = self._old().get_model(APP, "Match")
        # Reversal hands the stream back to the match column, so the old code
        # dedupes against the highest seq any phone had applied.
        assert old_match._base_manager.get(pk=live).live_applied_seq == 7
        assert old_match._base_manager.get(pk=finished).live_applied_seq == 9

        _migrate_to(LIVE_SCORER)
        assert LiveScorer.objects.get(match_id=live).applied_seq == 7
        assert Match.all_objects.get(pk=live).live_rev == 0

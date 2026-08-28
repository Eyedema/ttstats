"""Backfilling every pre-tenancy row into a club (KAN-13 / S1.2, KAN-26).

The failure this suite exists to prevent: a row left with a null club after
S1.3's managers start filtering is invisible to everyone, and users read that
as data loss. So the assertions are mostly "zero nulls" and "row counts
unchanged", not "the code ran".
"""
import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from io import StringIO

from pingpong import club_backfill
from pingpong.management.commands.backfill_clubs import command_models
from pingpong.models import (
    Championship,
    Club,
    ClubMembership,
    Location,
    Match,
    Player,
    ScheduledMatch,
)
from pingpong.services import set_match_sides

from .conftest import (
    ChampionshipFactory,
    ClubFactory,
    GameFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
)


def _run(**kwargs):
    return club_backfill.run(command_models(), **kwargs)


def _counts():
    """Application row counts. These must not move."""
    from pingpong.models import Game

    return {
        "player": Player.objects.count(),
        "match": Match.all_objects.count(),
        "scheduled": ScheduledMatch.all_objects.count(),
        "championship": Championship.all_objects.count(),
        "location": Location.objects.count(),
        "game": Game.all_objects.count(),
        "user": User.objects.count(),
    }


def _null_club_counts():
    return {
        "player": Player.objects.filter(club__isnull=True).count(),
        "match": Match.all_objects.filter(club__isnull=True).count(),
        "scheduled": ScheduledMatch.all_objects.filter(club__isnull=True).count(),
        "championship": Championship.all_objects.filter(club__isnull=True).count(),
        "location": Location.objects.filter(club__isnull=True).count(),
    }


@pytest.fixture
def mixed_estate(db):
    """A spread of pre-tenancy data, all of it with a null club."""
    users = [UserFactory() for _ in range(3)]
    players = [PlayerFactory(user=u) for u in users]
    players += [PlayerFactory() for _ in range(3)]  # guests, no user
    LocationFactory()
    LocationFactory()

    m1 = MatchFactory(player1=players[0], player2=players[1])
    GameFactory(match=m1, game_number=1, team1_score=11, team2_score=5)
    MatchFactory(
        is_double=True,
        team1_players=[players[0], players[1]],
        team2_players=[players[2], players[3]],
    )
    ScheduledMatchFactory(player1=players[0], player2=players[2])
    ChampionshipFactory(
        with_entries=[[players[0]], [players[1]]], created_by=players[0]
    )
    return players


@pytest.mark.django_db
class TestBackfillAssignsEverything:
    def test_no_row_has_a_null_club_afterwards(self, mixed_estate):
        assert any(v for v in _null_club_counts().values())
        _run()
        assert _null_club_counts() == {
            "player": 0,
            "match": 0,
            "scheduled": 0,
            "championship": 0,
            "location": 0,
        }

    def test_row_counts_are_identical_before_and_after(self, mixed_estate):
        """This migration assigns. It never creates or deletes."""
        before = _counts()
        _run()
        assert _counts() == before

    def test_the_default_club_is_created_once(self, mixed_estate):
        _run()
        _run()
        assert Club.objects.filter(slug=club_backfill.DEFAULT_CLUB_SLUG).count() == 1

    def test_every_user_gets_a_membership(self, mixed_estate):
        """Without this the data is assigned and the app is still empty for
        everyone — S1.4 resolves the active club from memberships."""
        _run()
        for user in User.objects.all():
            assert user.club_memberships.exists(), user.username

    def test_a_players_club_is_the_default(self, mixed_estate):
        _run()
        default = Club.objects.get(slug=club_backfill.DEFAULT_CLUB_SLUG)
        assert set(Player.objects.values_list("club_id", flat=True)) == {default.pk}


@pytest.mark.django_db
class TestIdempotency:
    def test_a_second_run_reports_zero_updates(self, mixed_estate):
        first = _run()
        assert first.total_updated > 0

        second = _run()
        assert second.total_updated == 0
        assert second.club_created is False

    def test_a_second_run_changes_nothing(self, mixed_estate):
        _run()
        snapshot = {
            "players": dict(Player.objects.values_list("pk", "club_id")),
            "matches": dict(Match.all_objects.values_list("pk", "club_id")),
            "memberships": ClubMembership.objects.count(),
        }
        _run()
        assert dict(Player.objects.values_list("pk", "club_id")) == snapshot["players"]
        assert dict(Match.all_objects.values_list("pk", "club_id")) == snapshot["matches"]
        assert ClubMembership.objects.count() == snapshot["memberships"]

    def test_an_already_assigned_row_is_never_reassigned(self, mixed_estate):
        """A row that already belongs somewhere is not the backfill's business."""
        other = ClubFactory(name="Already Theirs")
        player = Player.objects.first()
        Player.objects.filter(pk=player.pk).update(club=other)

        _run()

        player.refresh_from_db()
        assert player.club == other


@pytest.mark.django_db
class TestDerivation:
    def test_a_matchs_club_comes_from_its_participants_not_its_creator(self):
        """A match is routinely entered by one player on behalf of others, so
        the creator is not evidence of whose data it is."""
        club = ClubFactory()
        p1 = PlayerFactory(club=club)
        p2 = PlayerFactory(club=club)
        match = MatchFactory(player1=p1, player2=p2)

        _run()

        match.refresh_from_db()
        assert match.club == club

    def test_a_scheduled_matchs_club_comes_from_its_participants(self):
        club = ClubFactory()
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)
        scheduled = ScheduledMatchFactory(player1=p1, player2=p2)

        _run()

        scheduled.refresh_from_db()
        assert scheduled.club == club

    def test_a_championships_club_comes_from_its_entry_members(self):
        club = ClubFactory()
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)
        champ = ChampionshipFactory(with_entries=[[p1], [p2]])

        _run()

        champ.refresh_from_db()
        assert champ.club == club

    def test_a_championship_with_no_entries_falls_back_to_its_creator(self):
        club = ClubFactory()
        creator = PlayerFactory(club=club)
        champ = ChampionshipFactory(created_by=creator)

        _run()

        champ.refresh_from_db()
        assert champ.club == club

    def test_a_membership_follows_the_users_player_club(self):
        club = ClubFactory()
        player = PlayerFactory(with_user=True, club=club)

        _run()

        assert player.user.club_memberships.get().club == club


@pytest.mark.django_db
class TestEdgeCases:
    def test_a_match_with_no_participants_is_assigned_and_logged(self):
        """MatchParticipant.player is CASCADE, so deleting a player empties a
        side — a documented production incident. Such a match still needs a
        club or the scoping filters make it permanently invisible."""
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)
        match.participants.all().delete()

        report = _run()

        match.refresh_from_db()
        assert match.club is not None
        assert any(f"match {match.pk}" in a for a in report.anomalies)

    def test_a_cross_club_match_is_assigned_and_logged_not_skipped(self):
        """The anomaly is worth a human's attention. It is not a reason to
        abort a backfill halfway through."""
        club_a, club_b = ClubFactory(name="A"), ClubFactory(name="B")
        p1 = PlayerFactory(club=club_a)
        p2, p3 = PlayerFactory(club=club_b), PlayerFactory(club=club_b)
        match = MatchFactory(
            is_double=True, team1_players=[p1, p2], team2_players=[p3, PlayerFactory(club=club_b)]
        )

        report = _run()

        match.refresh_from_db()
        assert match.club == club_b  # the majority
        assert any("spans clubs" in a for a in report.anomalies)

    def test_cross_club_resolution_is_deterministic_across_runs(self):
        club_a, club_b = ClubFactory(name="A"), ClubFactory(name="B")
        p1, p2 = PlayerFactory(club=club_a), PlayerFactory(club=club_b)
        match = MatchFactory(player1=p1, player2=p2)

        _run()
        match.refresh_from_db()
        first = match.club_id

        club_backfill.reverse(command_models())
        Player.objects.filter(pk=p1.pk).update(club=club_a)
        Player.objects.filter(pk=p2.pk).update(club=club_b)
        _run()
        match.refresh_from_db()

        assert match.club_id == first

    def test_a_player_with_no_user_and_no_matches_gets_the_default(self):
        orphan = PlayerFactory()
        _run()
        orphan.refresh_from_db()
        assert orphan.club.slug == club_backfill.DEFAULT_CLUB_SLUG

    def test_a_user_with_no_player_still_gets_a_membership(self):
        """base.html needs a Player, but a half-set-up account must not be left
        with no club — it would see an empty app with no way out."""
        user = UserFactory()
        Player.objects.filter(user=user).delete()

        _run()

        assert user.club_memberships.count() == 1

    def test_an_empty_database_is_handled(self):
        report = _run()
        assert report.total_updated == 0
        assert Club.objects.filter(slug=club_backfill.DEFAULT_CLUB_SLUG).exists()


@pytest.mark.django_db
class TestReverse:
    def test_reverse_clears_every_assignment(self, mixed_estate):
        _run()
        club_backfill.reverse(command_models())

        counts = _null_club_counts()
        assert counts["player"] == Player.objects.count()
        assert counts["match"] == Match.all_objects.count()
        assert counts["championship"] == Championship.all_objects.count()

    def test_reverse_removes_the_default_club_and_its_memberships(self, mixed_estate):
        _run()
        club_backfill.reverse(command_models())

        assert not Club.objects.filter(slug=club_backfill.DEFAULT_CLUB_SLUG).exists()
        assert ClubMembership.objects.count() == 0

    def test_reverse_loses_no_application_rows(self, mixed_estate):
        before = _counts()
        _run()
        club_backfill.reverse(command_models())
        assert _counts() == before

    def test_the_backfill_can_be_re_run_after_a_reverse(self, mixed_estate):
        _run()
        club_backfill.reverse(command_models())
        report = _run()
        assert report.total_updated > 0
        assert all(v == 0 for v in _null_club_counts().values())


@pytest.mark.django_db
class TestBackfillClubsCommand:
    def _call(self, *args):
        out = StringIO()
        call_command("backfill_clubs", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self, mixed_estate):
        before = _null_club_counts()
        clubs_before = Club.objects.count()
        memberships_before = ClubMembership.objects.count()

        output = self._call("--dry-run")

        assert "DRY RUN" in output
        assert _null_club_counts() == before
        assert Club.objects.count() == clubs_before
        assert ClubMembership.objects.count() == memberships_before

    def test_dry_run_on_a_virgin_database_does_not_create_the_default_club(self):
        """Asking what a command *would* do must not be how the club gets made.

        Note the test database already carries the club that migration 0033
        creates, so this removes it first to reach the virgin state.
        """
        Club.objects.filter(slug=club_backfill.DEFAULT_CLUB_SLUG).delete()
        PlayerFactory()

        self._call("--dry-run")

        assert not Club.objects.filter(
            slug=club_backfill.DEFAULT_CLUB_SLUG
        ).exists()

    def test_dry_run_reports_what_it_would_change(self, mixed_estate):
        output = self._call("--dry-run")
        assert "Would update" in output
        assert "player" in output

    def test_the_command_assigns_everything(self, mixed_estate):
        self._call()
        assert all(v == 0 for v in _null_club_counts().values())

    def test_running_twice_reports_nothing_to_do(self, mixed_estate):
        self._call()
        output = self._call()
        assert "Nothing to do" in output

    def test_the_summary_names_examined_updated_and_skipped(self, mixed_estate):
        output = self._call()
        assert "examined" in output and "updated" in output and "skipped" in output

    def test_anomalies_are_surfaced_in_the_summary(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)
        match.participants.all().delete()

        output = self._call()
        assert "Anomalies" in output
        assert "no participants" in output

    def test_a_batch_size_smaller_than_the_data_still_assigns_everything(
        self, mixed_estate
    ):
        self._call("--batch-size", "1")
        assert all(v == 0 for v in _null_club_counts().values())

    def test_a_zero_batch_size_is_rejected(self, mixed_estate):
        output = self._call("--batch-size", "0")
        assert "at least 1" in output

    def test_the_command_reaches_rows_row_level_security_would_hide(self):
        """A management command has no user in thread-local context, so
        Match.objects would be filtered by a user that does not exist."""
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)

        self._call()

        match.refresh_from_db()
        assert match.club is not None

    def test_live_matches_are_assigned_too(self):
        """Match.objects excludes is_live=True. A live match with a null club
        would become invisible to its own scorekeeper the moment it finished."""
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)
        Match.all_objects.filter(pk=match.pk).update(is_live=True)

        self._call()

        match.refresh_from_db()
        assert match.club is not None

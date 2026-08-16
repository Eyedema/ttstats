import pytest
from datetime import date, timedelta

from django.test import Client
from django.urls import reverse

from pingpong.models import Championship, ScheduledMatch, Match
from .conftest import (
    ChampionshipFactory,
    GameFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    UserFactory,
    confirm_match,
)


def _player_with_verified_user():
    """Create a player with a verified user (needed for template rendering)."""
    player = PlayerFactory(with_user=True)
    player.user.profile.email_verified = True
    player.user.profile.save()
    return player


def _auth_client(user):
    """Create a logged-in test client."""
    c = Client()
    c.force_login(user)
    return c


def _singles_team(player):
    return [player]


def _make_participants(n=4):
    players = [_player_with_verified_user() for _ in range(n)]
    teams = [[p] for p in players]
    return players, teams


# ---------------------------------------------------------------------------
# Championship List View
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipListView:
    def test_list_requires_login(self):
        resp = Client().get(reverse("pingpong:championship_list"))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_list_shows_public_championships(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        ChampionshipFactory(name="Public Cup", is_public=True)
        resp = client.get(reverse("pingpong:championship_list"))
        assert resp.status_code == 200
        assert "Public Cup" in resp.content.decode()

    def test_list_hides_private_championships_from_non_participants(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        ChampionshipFactory(name="Secret Cup", is_public=False)
        resp = client.get(reverse("pingpong:championship_list"))
        assert "Secret Cup" not in resp.content.decode()

    def test_list_filter_by_status(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        ChampionshipFactory(name="Active Cup", status=Championship.Status.IN_PROGRESS)
        ChampionshipFactory(name="Done Cup", status=Championship.Status.COMPLETED)
        resp = client.get(reverse("pingpong:championship_list") + "?status=in_progress")
        content = resp.content.decode()
        assert "Active Cup" in content
        assert "Done Cup" not in content


# ---------------------------------------------------------------------------
# Championship Detail View
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipDetailView:
    def test_detail_requires_login(self):
        champ = ChampionshipFactory()
        resp = Client().get(reverse("pingpong:championship_detail", args=[champ.pk]))
        assert resp.status_code == 302

    def test_detail_shows_public_championship(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory(name="Detail Cup")
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        assert resp.status_code == 200
        assert "Detail Cup" in resp.content.decode()

    def test_detail_context_has_can_edit(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory(created_by=player)
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        assert resp.context['can_edit'] is True

    def test_detail_context_can_edit_false_for_non_creator(self):
        player = _player_with_verified_user()
        other = _player_with_verified_user()
        client = _auth_client(other.user)
        champ = ChampionshipFactory(created_by=player)
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        assert resp.context['can_edit'] is False

    def test_detail_shows_standings(self):
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        client = _auth_client(players[0].user)
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        assert 'standings' in resp.context
        assert len(resp.context['standings']) == 3

    def test_detail_private_404_for_non_participant(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory(is_public=False)
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Championship Results Matrix
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipResultsMatrix:
    def test_matrix_in_context(self):
        """Championship detail includes matrix_rows when matches are confirmed."""
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(with_participants=teams)
        m = MatchFactory(team1_players=[players[0]], team2_players=[players[1]], championship=champ)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=9)
        m.refresh_from_db()
        confirm_match(m)

        client = _auth_client(players[0].user)
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        assert 'matrix_rows' in resp.context
        assert len(resp.context['matrix_rows']) == 3

    def test_matrix_empty_no_confirmed_matches(self):
        """Matrix rows exist but all cells are pending when no matches confirmed."""
        players, teams = _make_participants(2)
        champ = ChampionshipFactory(with_participants=teams)
        client = _auth_client(players[0].user)
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        matrix_rows = resp.context['matrix_rows']
        for row in matrix_rows:
            for cell in row['cells']:
                assert cell.get('self') or cell.get('pending')

    def test_matrix_score_correct(self):
        """Matrix shows correct game score from row team's perspective."""
        players, teams = _make_participants(2)
        champ = ChampionshipFactory(with_participants=teams)
        m = MatchFactory(team1_players=[players[0]], team2_players=[players[1]], championship=champ)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=9)
        m.refresh_from_db()
        confirm_match(m)

        client = _auth_client(players[0].user)
        resp = client.get(reverse("pingpong:championship_detail", args=[champ.pk]))
        matrix_rows = resp.context['matrix_rows']
        # Find team0's row, check for the score cell
        for row in matrix_rows:
            if row['team'] == teams[0]:
                for cell in row['cells']:
                    if cell.get('score'):
                        assert cell['score'] == '3-0'
                        assert cell['won'] is True


# ---------------------------------------------------------------------------
# Championship Create View
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipCreateView:
    def test_create_requires_login(self):
        resp = Client().get(reverse("pingpong:championship_create"))
        assert resp.status_code == 302

    def test_create_get_renders_form(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        resp = client.get(reverse("pingpong:championship_create"))
        assert resp.status_code == 200

    def test_create_public_championship(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        LocationFactory()
        resp = client.post(reverse("pingpong:championship_create"), {
            "name": "New Cup",
            "championship_type": "singles",
            "is_public": True,
            "max_participants": 8,
            "start_date": (date.today() + timedelta(days=14)).isoformat(),
            "registration_deadline": (date.today() + timedelta(days=7)).isoformat(),
        })
        assert resp.status_code == 302
        assert Championship.objects.filter(name="New Cup").exists()
        champ = Championship.objects.get(name="New Cup")
        assert champ.created_by == player
        assert champ.status == Championship.Status.REGISTRATION


# ---------------------------------------------------------------------------
# Championship Edit View
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipEditView:
    def test_edit_requires_login(self):
        champ = ChampionshipFactory()
        resp = Client().get(reverse("pingpong:championship_edit", args=[champ.pk]))
        assert resp.status_code == 302

    def test_edit_by_creator(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory(created_by=player, name="Old Name")
        resp = client.post(reverse("pingpong:championship_edit", args=[champ.pk]), {
            "name": "New Name",
            "description": "",
            "status": "registration",
        })
        assert resp.status_code == 302
        champ.refresh_from_db()
        assert champ.name == "New Name"


# ---------------------------------------------------------------------------
# Championship Register / Unregister
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipRegistration:
    def test_register_creates_an_entry(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory()
        resp = client.post(
            reverse("pingpong:championship_register", args=[champ.pk]), {}
        )
        assert resp.status_code == 302
        assert champ.entries.count() == 1
        assert list(champ.entries.first().players) == [player]

    def test_registering_twice_is_rejected(self):
        player = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory()
        url = reverse("pingpong:championship_register", args=[champ.pk])
        client.post(url, {})
        client.post(url, {})
        assert champ.entries.count() == 1

    def test_doubles_registration_requires_a_partner(self):
        from pingpong.models import Championship as C

        player = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory(championship_type=C.ChampionshipType.DOUBLES)
        resp = client.post(
            reverse("pingpong:championship_register", args=[champ.pk]), {}
        )
        assert resp.status_code == 302
        assert champ.entries.count() == 0

    def test_doubles_registration_with_a_partner(self):
        from pingpong.models import Championship as C

        player = _player_with_verified_user()
        partner = _player_with_verified_user()
        client = _auth_client(player.user)
        champ = ChampionshipFactory(championship_type=C.ChampionshipType.DOUBLES)
        resp = client.post(
            reverse("pingpong:championship_register", args=[champ.pk]),
            {"partner": partner.pk},
        )
        assert resp.status_code == 302
        assert champ.entries.count() == 1
        assert set(champ.entries.first().players) == {player, partner}

    def test_unregister_entry(self):
        player = _player_with_verified_user()
        champ = ChampionshipFactory(with_entries=[[player]])
        client = _auth_client(player.user)
        resp = client.post(
            reverse("pingpong:championship_unregister", args=[champ.pk])
        )
        assert resp.status_code == 302
        assert not champ.entries.filter(members__player=player).exists()

    def test_unregister_blocked_after_registration_phase(self):
        player = _player_with_verified_user()
        champ = ChampionshipFactory(
            status=Championship.Status.SCHEDULED, with_entries=[[player]]
        )
        client = _auth_client(player.user)
        resp = client.post(
            reverse("pingpong:championship_unregister", args=[champ.pk])
        )
        assert resp.status_code == 302
        # Entry should still be registered
        assert champ.entries.filter(members__player=player).exists()


# ---------------------------------------------------------------------------
# Championship Start View
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipStartView:
    def test_start_generates_schedule(self):
        player = _player_with_verified_user()
        _, teams = _make_participants(3)
        champ = ChampionshipFactory(
            created_by=player,
            with_participants=teams,
        )
        client = _auth_client(player.user)
        resp = client.post(reverse("pingpong:championship_start", args=[champ.pk]))
        assert resp.status_code == 302
        champ.refresh_from_db()
        assert champ.status == Championship.Status.SCHEDULED
        assert ScheduledMatch.all_objects.filter(championship=champ).count() == 6

    def test_start_requires_creator(self):
        creator = _player_with_verified_user()
        other = _player_with_verified_user()
        _, teams = _make_participants(3)
        champ = ChampionshipFactory(created_by=creator, with_participants=teams)
        client = _auth_client(other.user)
        resp = client.post(reverse("pingpong:championship_start", args=[champ.pk]))
        assert resp.status_code == 302
        champ.refresh_from_db()
        assert champ.status == Championship.Status.REGISTRATION  # Should NOT have changed

    def test_start_requires_minimum_participants(self):
        player = _player_with_verified_user()
        team = _singles_team(player)
        champ = ChampionshipFactory(created_by=player, with_participants=[team])
        client = _auth_client(player.user)
        resp = client.post(reverse("pingpong:championship_start", args=[champ.pk]))
        assert resp.status_code == 302
        champ.refresh_from_db()
        assert champ.status == Championship.Status.REGISTRATION  # Should NOT have changed


# ---------------------------------------------------------------------------
# Scheduled Match Edit View
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestScheduledMatchEditView:
    def test_edit_requires_login(self):
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(created_by=players[0], with_participants=teams)
        champ.generate_schedule()
        sm = ScheduledMatch.all_objects.filter(championship=champ).first()
        resp = Client().get(reverse("pingpong:scheduled_match_edit", args=[sm.pk]))
        assert resp.status_code == 302

    def test_edit_by_organizer(self):
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(created_by=players[0], with_participants=teams)
        champ.generate_schedule()
        sm = ScheduledMatch.all_objects.filter(championship=champ).first()
        client = _auth_client(players[0].user)

        new_date = (date.today() + timedelta(days=30)).isoformat()
        resp = client.post(reverse("pingpong:scheduled_match_edit", args=[sm.pk]), {
            "scheduled_date": new_date,
            "scheduled_time": "19:00",
        })
        assert resp.status_code == 302
        sm.refresh_from_db()
        assert sm.scheduled_date.isoformat() == new_date

    def test_edit_blocked_for_non_organizer(self):
        players, teams = _make_participants(3)
        champ = ChampionshipFactory(created_by=players[0], with_participants=teams)
        champ.generate_schedule()
        sm = ScheduledMatch.all_objects.filter(championship=champ).first()
        client = _auth_client(players[1].user)

        resp = client.get(reverse("pingpong:scheduled_match_edit", args=[sm.pk]))
        assert resp.status_code == 302  # Redirected with error


# ---------------------------------------------------------------------------
# Championship Match Lifecycle (Integration)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChampionshipMatchLifecycle:
    def test_conversion_propagates_championship_fk(self):
        """When a championship scheduled match is converted, the resulting
        Match should have the championship FK set and match_type='tournament'."""
        players, teams = _make_participants(2)
        creator = players[0]
        champ = ChampionshipFactory(
            created_by=creator,
            with_participants=teams,
            status=Championship.Status.SCHEDULED,
        )
        champ.generate_schedule()
        sm = ScheduledMatch.all_objects.filter(championship=champ).first()

        client = _auth_client(creator.user)

        # Convert the scheduled match
        resp = client.post(
            reverse("pingpong:scheduled_match_convert", args=[sm.pk]),
            {
                "is_double": False,
                "player1": sm.side1_players.first().pk,
                "player2": sm.side2_players.first().pk,
                "date_played": date.today().isoformat(),
                "match_type": "casual",
                "best_of": 5,
            },
        )
        assert resp.status_code == 302

        sm.refresh_from_db()
        assert sm.match is not None
        match = sm.match
        assert match.championship == champ
        assert match.match_type == "tournament"

    def test_conversion_transitions_scheduled_to_in_progress(self):
        """First match conversion should transition championship to in_progress."""
        players, teams = _make_participants(2)
        creator = players[0]
        champ = ChampionshipFactory(
            created_by=creator,
            with_participants=teams,
            status=Championship.Status.SCHEDULED,
        )
        champ.generate_schedule()
        sm = ScheduledMatch.all_objects.filter(championship=champ).first()

        client = _auth_client(creator.user)
        client.post(
            reverse("pingpong:scheduled_match_convert", args=[sm.pk]),
            {
                "is_double": False,
                "player1": sm.side1_players.first().pk,
                "player2": sm.side2_players.first().pk,
                "date_played": date.today().isoformat(),
                "match_type": "casual",
                "best_of": 5,
            },
        )

        champ.refresh_from_db()
        assert champ.status == Championship.Status.IN_PROGRESS

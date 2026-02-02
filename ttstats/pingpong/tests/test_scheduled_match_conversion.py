import pytest
from datetime import date, time, timedelta
from django.urls import reverse
from django.contrib.messages import get_messages

from pingpong.models import Match, ScheduledMatch
from .conftest import UserFactory, PlayerFactory, LocationFactory, ScheduledMatchFactory, MatchFactory, TeamFactory


@pytest.mark.django_db
class TestScheduledMatchModel:
    """Test model-level conversion tracking"""

    def test_is_converted_false_by_default(self):
        """Scheduled match should not be converted by default"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        assert sm.is_converted is False

    def test_is_converted_true_when_linked(self):
        """Scheduled match should be converted when linked to a match"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)
        match = MatchFactory(player1=p1, player2=p2)

        sm.match = match
        sm.save()

        assert sm.is_converted is True

    def test_is_fully_confirmed_false_when_not_converted(self):
        """Should not be fully confirmed if not converted"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        assert sm.is_fully_confirmed is False

    def test_is_fully_confirmed_false_when_match_not_confirmed(self):
        """Should not be fully confirmed if match exists but not confirmed"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        # Verify the users' emails so confirmation is required
        p1.user.profile.email_verified = True
        p1.user.profile.save()
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        sm = ScheduledMatchFactory(player1=p1, player2=p2)
        match = MatchFactory(player1=p1, player2=p2)  # Not confirmed

        sm.match = match
        sm.save()

        assert sm.is_fully_confirmed is False

    def test_is_fully_confirmed_true_when_linked_and_confirmed(self):
        """Should be fully confirmed when linked to confirmed match"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)
        match = MatchFactory(player1=p1, player2=p2, confirmed=True)

        sm.match = match
        sm.save()

        assert sm.is_fully_confirmed is True


@pytest.mark.django_db
class TestScheduledMatchDetailView:
    """Test detail view access and context"""

    def test_requires_login(self, client):
        """Should require login to view scheduled match details"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        url = reverse("pingpong:scheduled_match_detail", kwargs={"pk": sm.pk})
        response = client.get(url)

        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_participant_can_view(self, client):
        """Participant should be able to view scheduled match"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_detail", kwargs={"pk": sm.pk})
        response = client.get(url)

        assert response.status_code == 200
        assert "scheduled_match" in response.context

    def test_shows_conversion_status(self, client):
        """Should show conversion status in context"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_detail", kwargs={"pk": sm.pk})
        response = client.get(url)

        assert response.context["is_converted"] is False
        assert response.context["is_fully_confirmed"] is False
        assert response.context["can_convert"] is True

    def test_shows_converted_status_when_linked(self, client):
        """Should show converted status when match is linked"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        # Verify emails so confirmation is required
        p1.user.profile.email_verified = True
        p1.user.profile.save()
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        sm = ScheduledMatchFactory(player1=p1, player2=p2)
        match = MatchFactory(player1=p1, player2=p2)
        sm.match = match
        sm.save()

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_detail", kwargs={"pk": sm.pk})
        response = client.get(url)

        assert response.context["is_converted"] is True
        assert response.context["is_fully_confirmed"] is False


@pytest.mark.django_db
class TestScheduledMatchConvertView:
    """Test conversion view logic"""

    def test_requires_login(self, client):
        """Should require login to convert scheduled match"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})
        response = client.get(url)

        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_participant_can_access_form(self, client):
        """Participant should be able to access conversion form"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        location = LocationFactory()
        sm = ScheduledMatchFactory(player1=p1, player2=p2, location=location)

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})
        response = client.get(url)

        assert response.status_code == 200
        assert "scheduled_match" in response.context
        assert response.context["scheduled_match"] == sm

    def test_form_prefilled_with_scheduled_match_data(self, client):
        """Form should be pre-filled with scheduled match data"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        location = LocationFactory()
        notes = "Test notes for scheduled match"
        sm = ScheduledMatchFactory(
            player1=p1,
            player2=p2,
            location=location,
            notes=notes,
            scheduled_date=date.today(),
            scheduled_time=time(14, 30)
        )

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})
        response = client.get(url)

        form = response.context["form"]
        assert form.initial["player1"] == p1
        assert form.initial["player2"] == p2
        assert form.initial["location"] == location
        assert form.initial["notes"] == notes

    def test_creates_match_and_links_to_scheduled_match(self, client):
        """Should create match and link it to scheduled match"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        location = LocationFactory()
        sm = ScheduledMatchFactory(player1=p1, player2=p2, location=location)

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})

        # Submit conversion form
        response = client.post(url, {
            "is_double": False,
            "player1": p1.pk,
            "player2": p2.pk,
            "date_played": "2026-02-02T14:30",
            "location": location.pk,
            "match_type": "casual",
            "best_of": 5,
            "notes": "Converted from scheduled match"
        })

        # Should redirect to match detail
        assert response.status_code == 302

        # Refresh scheduled match from DB
        sm.refresh_from_db()

        # Should be linked to a match
        assert sm.match is not None
        assert sm.is_converted is True

        # Match should have correct data
        match = sm.match
        assert match.team1.players.first() == p1
        assert match.team2.players.first() == p2
        assert match.location == location
        assert match.match_type == "casual"
        assert match.best_of == 5
        assert match.is_double is False

    def test_redirects_if_already_converted(self, client):
        """Should redirect if scheduled match already converted"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)
        match = MatchFactory(player1=p1, player2=p2)
        sm.match = match
        sm.save()

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})
        response = client.get(url)

        # Should redirect to match detail
        assert response.status_code == 302
        assert f"/matches/{match.pk}/" in response.url

        # Should have info message
        messages = list(get_messages(response.wsgi_request))
        assert any("already been converted" in str(m) for m in messages)

    def test_non_participant_cannot_convert(self, client):
        """Non-participant should not be able to convert"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        p3 = PlayerFactory(with_user=True)  # Not in match
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        client.force_login(p3.user)
        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})
        response = client.get(url)

        # Should redirect with error
        assert response.status_code == 302
        messages = list(get_messages(response.wsgi_request))
        assert any("don't have permission" in str(m) for m in messages)

    def test_staff_can_convert_any_scheduled_match(self, client):
        """Staff should be able to convert any scheduled match"""
        staff_user = UserFactory(is_staff=True)
        staff_player = PlayerFactory(user=staff_user)
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        location = LocationFactory()
        sm = ScheduledMatchFactory(player1=p1, player2=p2, location=location)

        client.force_login(staff_user)
        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})
        response = client.get(url)

        assert response.status_code == 200
        assert "scheduled_match" in response.context

    def test_converts_doubles_match_correctly(self, client):
        """Should handle doubles match conversion with 4 players"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        p3 = PlayerFactory(with_user=True)
        p4 = PlayerFactory(with_user=True)

        team1 = TeamFactory(players=[p1, p2])
        team2 = TeamFactory(players=[p3, p4])
        location = LocationFactory()

        sm = ScheduledMatchFactory(team1=team1, team2=team2, location=location)

        client.force_login(p1.user)
        url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})

        # Submit conversion form for doubles
        response = client.post(url, {
            "is_double": True,
            "player1": p1.pk,
            "player2": p3.pk,
            "player3": p2.pk,
            "player4": p4.pk,
            "date_played": "2026-02-02T14:30",
            "location": location.pk,
            "match_type": "casual",
            "best_of": 5,
            "notes": "Doubles match"
        })

        # Should redirect to match detail
        assert response.status_code == 302

        # Refresh scheduled match from DB
        sm.refresh_from_db()

        # Should be linked to a match
        assert sm.match is not None
        assert sm.match.is_double is True

        # Teams should have correct players
        team1_players = set(sm.match.team1.players.all())
        team2_players = set(sm.match.team2.players.all())
        assert team1_players == {p1, p2}
        assert team2_players == {p3, p4}


@pytest.mark.django_db
class TestCalendarViewFiltering:
    """Test calendar visibility logic"""

    def test_shows_unconverted_scheduled_matches(self, client):
        """Calendar should show unconverted scheduled matches"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        today = date.today()
        sm = ScheduledMatchFactory(
            player1=p1,
            player2=p2,
            scheduled_date=today
        )

        client.force_login(p1.user)
        url = reverse("pingpong:calendar")
        response = client.get(url)

        assert response.status_code == 200

        # Check that scheduled match appears in calendar
        calendar_weeks = response.context["calendar_weeks"]
        all_matches = []
        for week in calendar_weeks:
            for day in week:
                all_matches.extend(day['matches'])

        # Should find the scheduled match
        scheduled_in_calendar = [m for m in all_matches if hasattr(m, 'is_scheduled') and m.is_scheduled]
        assert len(scheduled_in_calendar) > 0

    def test_hides_fully_confirmed_scheduled_matches(self, client):
        """Calendar should hide fully confirmed scheduled matches"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        today = date.today()

        # Create scheduled match and convert it
        sm = ScheduledMatchFactory(
            player1=p1,
            player2=p2,
            scheduled_date=today
        )
        match = MatchFactory(player1=p1, player2=p2, confirmed=True)
        sm.match = match
        sm.save()

        client.force_login(p1.user)
        url = reverse("pingpong:calendar")
        response = client.get(url)

        assert response.status_code == 200

        # Check that scheduled match does NOT appear in calendar
        calendar_weeks = response.context["calendar_weeks"]
        all_matches = []
        for week in calendar_weeks:
            for day in week:
                all_matches.extend(day['matches'])

        # Should not find the scheduled match (it's fully confirmed)
        scheduled_in_calendar = [m for m in all_matches if hasattr(m, 'is_scheduled') and m.is_scheduled and m.pk == sm.pk]
        assert len(scheduled_in_calendar) == 0

    def test_shows_converted_but_unconfirmed_scheduled_matches(self, client):
        """Calendar should still show converted matches that aren't confirmed"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        today = date.today()

        # Verify emails so confirmation is required
        p1.user.profile.email_verified = True
        p1.user.profile.save()
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        # Create scheduled match and convert it (but don't confirm)
        sm = ScheduledMatchFactory(
            player1=p1,
            player2=p2,
            scheduled_date=today
        )
        match = MatchFactory(player1=p1, player2=p2, confirmed=False)
        sm.match = match
        sm.save()

        client.force_login(p1.user)
        url = reverse("pingpong:calendar")
        response = client.get(url)

        assert response.status_code == 200

        # Check that scheduled match DOES appear (not fully confirmed yet)
        calendar_weeks = response.context["calendar_weeks"]
        all_matches = []
        for week in calendar_weeks:
            for day in week:
                all_matches.extend(day['matches'])

        # Should find the scheduled match (converted but not confirmed)
        scheduled_in_calendar = [m for m in all_matches if hasattr(m, 'is_scheduled') and m.is_scheduled and m.pk == sm.pk]
        assert len(scheduled_in_calendar) > 0


    def test_hides_played_match_when_scheduled_match_not_confirmed(self, client):
        """Calendar should only show scheduled match, not the played match, when unconfirmed"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        today = date.today()

        # Verify emails so confirmation is required
        p1.user.profile.email_verified = True
        p1.user.profile.save()
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        # Create scheduled match and convert it
        sm = ScheduledMatchFactory(
            player1=p1,
            player2=p2,
            scheduled_date=today
        )
        match = MatchFactory(player1=p1, player2=p2, confirmed=False)
        match.date_played = match.date_played.replace(year=today.year, month=today.month, day=today.day)
        match.save()
        sm.match = match
        sm.save()

        # Confirm as player 1 only (not player 2)
        from pingpong.models import MatchConfirmation
        MatchConfirmation.objects.create(player=p1, match=match)

        client.force_login(p1.user)
        url = reverse("pingpong:calendar")
        response = client.get(url)

        assert response.status_code == 200

        # Check what appears in calendar
        calendar_weeks = response.context["calendar_weeks"]
        all_matches = []
        for week in calendar_weeks:
            for day in week:
                all_matches.extend(day['matches'])

        # Should ONLY find the scheduled match, NOT the played match
        scheduled_matches = [m for m in all_matches if hasattr(m, 'is_scheduled') and m.is_scheduled]
        played_matches = [m for m in all_matches if hasattr(m, 'is_scheduled') and not m.is_scheduled]

        assert len(scheduled_matches) == 1, "Should show the scheduled match"
        assert len(played_matches) == 0, "Should NOT show the played match (it's linked to unconfirmed scheduled match)"
        assert scheduled_matches[0].pk == sm.pk


@pytest.mark.django_db
class TestConversionIntegration:
    """End-to-end workflow testing"""

    def test_full_conversion_workflow(self, client):
        """Test complete flow: schedule -> view detail -> convert -> add games -> confirm"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        location = LocationFactory()

        # Step 1: Create scheduled match
        sm = ScheduledMatchFactory(
            player1=p1,
            player2=p2,
            location=location,
            scheduled_date=date.today(),
            scheduled_time=time(14, 0)
        )

        # Step 2: View detail page
        client.force_login(p1.user)
        detail_url = reverse("pingpong:scheduled_match_detail", kwargs={"pk": sm.pk})
        response = client.get(detail_url)
        assert response.status_code == 200
        assert response.context["can_convert"] is True

        # Step 3: Convert to played match
        convert_url = reverse("pingpong:scheduled_match_convert", kwargs={"scheduled_match_pk": sm.pk})
        response = client.post(convert_url, {
            "is_double": False,
            "player1": p1.pk,
            "player2": p2.pk,
            "date_played": "2026-02-02T14:00",
            "location": location.pk,
            "match_type": "casual",
            "best_of": 5,
            "notes": "Great match!"
        })
        assert response.status_code == 302

        # Verify match was created and linked
        sm.refresh_from_db()
        assert sm.match is not None
        match = sm.match

        # Step 4: Add games (simulate via factory for simplicity)
        from .conftest import GameFactory
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)

        # Match should now have a winner
        match.refresh_from_db()
        assert match.winner is not None

        # Step 5: Confirm match (both players)
        confirm_url = reverse("pingpong:match_confirm", kwargs={"pk": match.pk})

        # Player 1 confirms
        client.force_login(p1.user)
        response = client.post(confirm_url)
        assert response.status_code == 302

        # Player 2 confirms
        client.force_login(p2.user)
        response = client.post(confirm_url)
        assert response.status_code == 302

        # Match should now be fully confirmed
        match.refresh_from_db()
        assert match.match_confirmed is True

        # Scheduled match should now be fully confirmed
        sm.refresh_from_db()
        assert sm.is_fully_confirmed is True

        # Step 6: Verify scheduled match no longer appears in calendar
        client.force_login(p1.user)
        calendar_url = reverse("pingpong:calendar")
        response = client.get(calendar_url)
        calendar_weeks = response.context["calendar_weeks"]
        all_matches = []
        for week in calendar_weeks:
            for day in week:
                all_matches.extend(day['matches'])

        # Should not find this scheduled match in calendar
        scheduled_in_calendar = [m for m in all_matches if hasattr(m, 'pk') and hasattr(m, 'is_scheduled') and m.is_scheduled and m.pk == sm.pk]
        assert len(scheduled_in_calendar) == 0

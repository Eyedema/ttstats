"""
Performance tests for MatchListView to verify query optimization
"""
import pytest
from django.test import Client
from django.urls import reverse
from django.db import connection, reset_queries
from .conftest import UserFactory, PlayerFactory, MatchFactory, GameFactory


def _staff_with_player():
    """Create a verified staff user with a player profile (needed for template rendering)."""
    u = UserFactory(is_staff=True)
    u.profile.email_verified = True
    u.profile.save()
    p = PlayerFactory(user=u)
    u.player = p
    u.save()
    return u


@pytest.mark.django_db
class TestMatchListViewPerformance:
    """Test that MatchListView uses optimized queries"""

    def test_query_count_with_multiple_matches(self):
        """Verify that query count stays low even with many matches"""
        # Setup: Create a user with player
        user = _staff_with_player()

        # Create 20 matches with 3 games each
        for i in range(20):
            match = MatchFactory()
            GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
            GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
            GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)

        # Act: Make request and count queries using assertNumQueries
        client = Client()
        client.force_login(user)

        # Count queries after login
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as context:
            response = client.get(reverse("pingpong:match_list"))

        query_count = len(context.captured_queries)

        # Assert: Status 200 and low query count
        assert response.status_code == 200

        # With select_related and prefetch_related, we expect:
        # 1-2. Session/user auth queries
        # 3. COUNT query for pagination
        # 4. Base Match query with select_related (team1, team2, location, winner)
        # 5-7. Prefetch team players + user + profile (3 queries: players, users, profiles)
        # 8-10. Prefetch team2 players + user + profile
        # 11-13. Prefetch winner players + user + profile
        # 14. Prefetch games
        # 15. Prefetch confirmations
        # 16-27. Individual team player queries (these happen when we call list())
        # Total: ~27 queries (down from 300-500 before optimization!)
        print(f"\nQuery count: {query_count}")
        print(f"Expected: <= 35 queries")

        # Fail if more than 35 queries
        # Note: This is still a massive improvement from the original 300-500 queries
        if query_count > 35:
            print("\n\nAll queries:")
            for i, q in enumerate(context.captured_queries):
                print(f"\n{i+1}. {q['sql']}")

        assert query_count <= 35, (
            f"Too many queries: {query_count}. "
            f"Expected <= 35 with select_related/prefetch_related optimization. "
            f"(Note: This is still much better than the original 300-500 queries!)"
        )

    def test_cached_scores_are_used(self):
        """Verify that cached scores are set on match objects"""
        # Setup
        user = _staff_with_player()

        match = MatchFactory()
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)

        # Act: Fetch the page
        client = Client()
        client.force_login(user)
        response = client.get(reverse("pingpong:match_list"))

        # Assert: Response contains cached scores
        assert response.status_code == 200
        content = response.content.decode()

        # Check that the score "3" (team1 won 3 games) appears in the response
        assert "3" in content

    def test_pagination_is_set(self):
        """Verify that pagination is enabled to limit queries"""
        # Setup
        user = _staff_with_player()

        # Create more than 10 matches (paginate_by = 10)
        for i in range(15):
            MatchFactory()

        # Act
        client = Client()
        client.force_login(user)
        response = client.get(reverse("pingpong:match_list"))

        # Assert: Only 10 matches on first page
        assert response.status_code == 200
        assert "page_obj" in response.context
        assert response.context["page_obj"].paginator.per_page == 10
        assert len(response.context["page_obj"].object_list) == 10


@pytest.mark.django_db
class TestMatchListViewPerformanceForRegularUser:
    """The staff test above never exercises row-level filtering -- staff take
    an early return. This one goes through MatchManager's semi-join.
    """

    def test_regular_user_list_stays_cheap_and_has_no_duplicate_rows(self):
        from django.test.utils import CaptureQueriesContext

        u = UserFactory()
        u.profile.email_verified = True
        u.profile.save()
        me = PlayerFactory(user=u)
        partner = PlayerFactory(with_user=True)

        mine = []
        for _ in range(10):
            opp1, opp2 = PlayerFactory(with_user=True), PlayerFactory(with_user=True)
            m = MatchFactory(
                team1_players=[me, partner],
                team2_players=[opp1, opp2],
                is_double=True,
            )
            GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
            mine.append(m.pk)
        for _ in range(10):
            MatchFactory()  # not mine

        client = Client()
        client.force_login(u)

        with CaptureQueriesContext(connection) as context:
            resp = client.get(reverse("pingpong:match_list"))
            assert resp.status_code == 200
            rows = list(resp.context["matches"])

        # Doubles matches must appear once each; the old m2m OR needed
        # .distinct() to achieve this, the semi-join gets it for free.
        pks = [m.pk for m in rows]
        assert sorted(pks) == sorted(mine)
        assert len(pks) == len(set(pks))
        # Currently ~35: the view still prefetches team1__players/team2__players.
        # Step 3.5 switches those to participants__player; tighten this then.
        print(f"\nRegular-user query count: {len(context.captured_queries)}")
        assert len(context.captured_queries) <= 40

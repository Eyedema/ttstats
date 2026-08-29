"""Club-scoped cache keys (KAN-16 / S1.5).

The cross-club *payload* poisoning tests live in `test_tenancy_isolation.py`,
where they belong with the rest of the security suite. This file covers the key
shapes underneath them -- the part that decides whether those tests can ever
fail for the right reason.

The specific thing being prevented: a deploy leaves old-format keys in Redis,
and a new-format request reads one. That must be impossible by construction,
not by TTL expiry.
"""
import pytest
from django.core.cache import cache

from pingpong import cache_utils
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import ClubFactory, MatchFactory, PlayerFactory, UserFactory


@pytest.fixture(autouse=True)
def _clean_context():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.fixture
def clubs_on(settings):
    settings.FEATURE_FLAGS = {"clubs": True}


@pytest.fixture
def clubs_off(settings):
    settings.FEATURE_FLAGS = {"clubs": False}


def _as_club(club):
    _thread_locals.user = UserFactory()
    _thread_locals.club = club


@pytest.mark.django_db
@pytest.mark.usefixtures("clubs_off")
class TestFlagOffKeysAreUnchanged:
    """With the flag off, key shapes and hit rates must be effectively
    unchanged. This is what production runs until the cutover."""

    def test_the_prefix_is_empty(self):
        assert cache_utils.club_prefix() == ""

    def test_the_prefix_is_empty_even_for_an_explicit_club(self):
        assert cache_utils.club_prefix(ClubFactory()) == ""

    def test_the_generation_key_keeps_its_pre_tenancy_name(self):
        assert cache_utils.leaderboard_generation_key() == "leaderboard_generation"

    def test_invalidation_bumps_the_single_global_counter(self):
        cache_utils.invalidate_leaderboard()
        assert cache.get("leaderboard_generation") == 1
        cache_utils.invalidate_leaderboard()
        assert cache.get("leaderboard_generation") == 2


@pytest.mark.django_db
@pytest.mark.usefixtures("clubs_on")
class TestKeysAreNamespacedByClub:
    def test_two_clubs_produce_different_prefixes(self):
        a, b = ClubFactory(), ClubFactory()
        assert cache_utils.club_prefix(a) != cache_utils.club_prefix(b)

    def test_the_club_id_is_a_prefix_not_a_suffix(self):
        """`cache_control --stats` groups by namespace, and `club7:*` is a
        scannable pattern where `*:club7` is not."""
        club = ClubFactory()
        assert cache_utils.club_prefix(club).startswith(f"club{club.pk}")
        assert cache_utils.club_prefix(club).endswith(":")

    def test_a_viewer_with_no_club_gets_its_own_namespace(self):
        """It must collide with neither a real club's keys nor the flag-off
        namespace — otherwise a clubless user is served the unscoped payload."""
        _thread_locals.user = UserFactory()
        prefix = cache_utils.club_prefix()

        assert prefix == f"{cache_utils.NO_CLUB}:"
        assert prefix != ""
        assert not prefix.startswith("club")

    def test_the_generation_key_is_per_club(self):
        a, b = ClubFactory(), ClubFactory()
        assert cache_utils.leaderboard_generation_key(
            a
        ) != cache_utils.leaderboard_generation_key(b)

    def test_the_new_generation_key_cannot_collide_with_the_old_one(self):
        """A deploy leaves old keys in Redis. A new-format request must never
        read one — a clean break, not an ambiguous overlap."""
        club = ClubFactory()
        assert cache_utils.leaderboard_generation_key(club) != "leaderboard_generation"


@pytest.mark.django_db
@pytest.mark.usefixtures("clubs_on")
class TestInvalidationIsPerClub:
    def test_bumping_club_a_does_not_bump_club_b(self):
        a, b = ClubFactory(), ClubFactory()
        cache_utils.invalidate_leaderboard(club=b)
        before = cache.get(cache_utils.leaderboard_generation_key(b))

        cache_utils.invalidate_leaderboard(club=a)

        assert cache.get(cache_utils.leaderboard_generation_key(b)) == before
        assert cache.get(cache_utils.leaderboard_generation_key(a)) == 1

    def test_a_players_caches_are_invalidated_in_their_own_club(self):
        club = ClubFactory()
        player = PlayerFactory(club=club)
        key = f"{cache_utils.club_prefix(club)}player_stats_{player.pk}"
        cache.set(key, "stale", 600)

        cache_utils.invalidate_player_caches(player)

        assert cache.get(key) is None

    def test_a_player_cache_key_does_not_depend_on_who_is_looking(self):
        """These are that player's stats, not a per-viewer view of them. A
        viewer-dependent key means one entry per viewer and a hit rate of ~0."""
        club, other = ClubFactory(), ClubFactory()
        player = PlayerFactory(club=club)

        _as_club(club)
        from_own_club = cache_utils.club_prefix(player.club)
        _as_club(other)
        from_other_club = cache_utils.club_prefix(player.club)

        assert from_own_club == from_other_club

    def test_a_cross_club_match_invalidates_both_clubs(self):
        """Representable by design (KAN-12). Half-invalidating one leaves the
        other side reading a stale table."""
        club_a, club_b = ClubFactory(), ClubFactory()
        p1 = PlayerFactory(club=club_a)
        p2 = PlayerFactory(club=club_b)
        match = MatchFactory(player1=p1, player2=p2)

        # Creating the match already fires the invalidation signals, so this
        # compares against the counters rather than assuming they start at 0.
        before_a = cache.get(cache_utils.leaderboard_generation_key(club_a), 0)
        before_b = cache.get(cache_utils.leaderboard_generation_key(club_b), 0)

        cache_utils.invalidate_match_caches(match)

        assert cache.get(cache_utils.leaderboard_generation_key(club_a)) > before_a
        assert cache.get(cache_utils.leaderboard_generation_key(club_b)) > before_b

    def test_invalidation_survives_a_match_whose_players_have_no_club(self):
        """Before the backfill completes, nothing has a club. Invalidation must
        not raise — a signal that throws turns a cache miss into a 500."""
        match = MatchFactory(player1=PlayerFactory(), player2=PlayerFactory())
        cache_utils.invalidate_match_caches(match)


@pytest.mark.django_db
@pytest.mark.usefixtures("clubs_on")
class TestWarmCacheWarmsPerClub:
    def test_it_warms_every_club(self):
        from io import StringIO

        from django.core.management import call_command

        a, b = ClubFactory(name="Alpha"), ClubFactory(name="Bravo")
        PlayerFactory(club=a)
        PlayerFactory(club=b)
        PlayerFactory(club=b)

        out = StringIO()
        call_command("warm_cache", stdout=out)

        assert cache.get(f"{cache_utils.club_prefix(a)}dashboard_total_players") == 1
        assert cache.get(f"{cache_utils.club_prefix(b)}dashboard_total_players") == 2

    def test_it_leaves_no_club_in_thread_local_context(self):
        """The command scopes by explicit filter, never by faking a request.

        Kept as an assertion rather than deleted: a future change that reaches
        for thread-local context to "simplify" this would reintroduce exactly
        the bug that made every club's warmed numbers identical.
        """
        from io import StringIO

        from django.core.management import call_command

        ClubFactory()
        call_command("warm_cache", stdout=StringIO())

        assert not hasattr(_thread_locals, "club")

    def test_each_clubs_warmed_numbers_are_actually_different(self):
        """The regression guard. When the command relied on thread-local
        context alone, every club got the whole database's count and this
        assertion was the only thing that noticed."""
        from io import StringIO

        from django.core.management import call_command

        a, b = ClubFactory(name="Alpha"), ClubFactory(name="Bravo")
        PlayerFactory(club=a)
        for _ in range(3):
            PlayerFactory(club=b)

        call_command("warm_cache", stdout=StringIO())

        assert cache.get(f"{cache_utils.club_prefix(a)}dashboard_total_players") == 1
        assert cache.get(f"{cache_utils.club_prefix(b)}dashboard_total_players") == 3


@pytest.mark.django_db
@pytest.mark.usefixtures("clubs_off")
class TestWarmCacheWithTheFlagOff:
    def test_it_does_one_unscoped_pass(self):
        from io import StringIO

        from django.core.management import call_command

        PlayerFactory()
        PlayerFactory()

        call_command("warm_cache", stdout=StringIO())

        assert cache.get("dashboard_total_players") == 2

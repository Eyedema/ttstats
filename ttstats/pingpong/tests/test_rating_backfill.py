"""The PlayerRating backfill (KAN-47): deterministic, idempotent, reversible."""
import importlib
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.apps import apps as global_apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from ..elo_replay import rebuild_player_ratings
from ..models import EloHistory, Player, PlayerRating, RatingChange, Side
from .conftest import ClubFactory, GameFactory, MatchFactory, PlayerFactory, SeasonFactory

backfill_migration = importlib.import_module("pingpong.migrations.0040_backfill_player_ratings")

BASE = datetime(2026, 1, 1, 12, tzinfo=dt_timezone.utc)


def _play(side1, side2, *, day, club=None, winner=Side.ONE):
    match = MatchFactory(
        team1_players=side1, team2_players=side2, is_double=len(side1) > 1,
        date_played=BASE + timedelta(days=day), club=club,
    )
    for n in (1, 2, 3):
        t1, t2 = (11, 4) if winner == Side.ONE else (4, 11)
        GameFactory(match=match, game_number=n, team1_score=t1, team2_score=t2)
    return match


def _history():
    """Live-confirmed in play order, with a season, a gap and doubles."""
    club = ClubFactory()
    SeasonFactory(club=club, starts_on=date(2026, 1, 5), ends_on=date(2026, 1, 20))
    a, b, c, d = (PlayerFactory(club=club) for _ in range(4))
    for day in range(30):
        if day % 7 == 6:
            _play([a, b], [c, d], day=day, club=club, winner=Side.TWO if day % 2 else Side.ONE)
        else:
            _play([a], [b if day % 2 else c], day=day, club=club,
                  winner=Side.ONE if day % 3 else Side.TWO)
    return a, b, c, d


def _key(row):
    return tuple((v is None, v if v is not None else 0) for v in row)


def _snapshot():
    ratings = PlayerRating.all_objects.values_list(
        "player_id", "club_id", "season_id", "format", "rating", "peak", "matches"
    )
    changes = RatingChange.objects.values_list(
        "match_id", "player_id", "rating__season_id", "rating__format",
        "old_rating", "new_rating", "rating_change", "k_factor",
    )
    return sorted(ratings, key=_key), sorted(changes, key=_key)


@pytest.mark.django_db
class TestBackfill:
    def test_rebuild_reproduces_what_the_live_pipeline_wrote(self):
        _history()
        live = _snapshot()
        assert live[0] and live[1]

        rebuild_player_ratings(chunk_size=7)

        assert _snapshot() == live

    def test_running_the_migration_twice_is_idempotent(self):
        _history()

        backfill_migration.backfill(global_apps, None)
        first = _snapshot()
        backfill_migration.backfill(global_apps, None)

        assert _snapshot() == first

    def test_legacy_fields_and_elo_history_are_untouched(self):
        _history()
        legacy = sorted(Player.objects.values_list("pk", "elo_rating", "elo_peak", "matches_for_elo"))
        history = EloHistory.objects.count()

        backfill_migration.backfill(global_apps, None)
        backfill_migration.unbackfill(global_apps, None)

        assert sorted(Player.objects.values_list("pk", "elo_rating", "elo_peak", "matches_for_elo")) == legacy
        assert EloHistory.objects.count() == history
        assert not PlayerRating.all_objects.exists()

    def test_singles_only_history_matches_legacy_exactly(self):
        """The acceptance criterion, on the backfill: a player who has only
        ever played singles (against singles-only opponents) has an all-time
        singles rating equal to today's value."""
        eve, fay = PlayerFactory(), PlayerFactory()
        for day in range(25):
            _play([eve], [fay], day=day, winner=Side.ONE if day % 4 else Side.TWO)
        PlayerRating.all_objects.all().delete()

        backfill_migration.backfill(global_apps, None)

        for player in (eve, fay):
            player.refresh_from_db()
            row = PlayerRating.all_objects.get(player=player, season=None, format="singles")
            assert (row.rating, row.peak, row.matches) == (
                player.elo_rating, player.elo_peak, player.matches_for_elo
            )

    def test_a_deleted_player_takes_their_rows_with_them(self):
        a, b, c, d = _history()
        b.delete()

        backfill_migration.backfill(global_apps, None)

        assert not PlayerRating.all_objects.filter(player_id=b.pk).exists()


def _migrate_to(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([("pingpong", target)])


def _leaf():
    executor = MigrationExecutor(connection)
    (leaf,) = [n for app, n in executor.loader.graph.leaf_nodes() if app == "pingpong"]
    return leaf


@pytest.fixture
def at_leaf():
    yield
    _migrate_to(_leaf())


@pytest.mark.django_db(transaction=True)
class TestRatingMigrationsAreTwoWay:
    def test_backfill_forward_reverse_and_forward_again(self, at_leaf):
        _history()
        live = _snapshot()

        _migrate_to("0039_player_rating")
        assert not PlayerRating.all_objects.exists()

        _migrate_to("0040_backfill_player_ratings")
        assert _snapshot() == live

        _migrate_to("0038_season")
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
        assert "pingpong_playerrating" not in tables
        assert "pingpong_ratingchange" not in tables
        assert Player.objects.count() == 4

        _migrate_to(_leaf())
        assert _snapshot() == live

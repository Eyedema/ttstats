"""Rebuilding PlayerRating/RatingChange from the history (`rating_replay`).

Migration 0040's backfill is a frozen no-op since KAN-73; what remains is
`rebuild_player_ratings`, which `recalculate_ratings` runs: deterministic,
equal to the live write, and keeping the frozen Elo history.
"""
from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from ..models import Player, PlayerRating, RatingChange, Side
from ..rating_replay import ELO_HISTORY_FIELDS, rebuild_player_ratings
from .conftest import (
    ClubFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    SeasonFactory,
)

BASE = datetime(2026, 1, 1, 12, tzinfo=UTC)


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


def _rounded(row):
    return tuple(round(v, 6) if isinstance(v, float) else v for v in row)


def _snapshot():
    ratings = PlayerRating.all_objects.values_list(
        "player_id", "club_id", "season_id", "format", "matches",
        "glicko_rating", "rd", "volatility", "last_rated_on",
    )
    changes = RatingChange.objects.values_list(
        "match_id", "player_id", "rating__season_id", "rating__format",
        "glicko_old_rating", "glicko_new_rating", "glicko_change",
        "glicko_rd_before", "glicko_rd_after", *ELO_HISTORY_FIELDS,
    )
    return (
        sorted((_rounded(r) for r in ratings), key=_key),
        sorted((_rounded(c) for c in changes), key=_key),
    )


@pytest.mark.django_db
class TestRebuild:
    def test_rebuild_reproduces_what_the_live_pipeline_wrote(self):
        _history()
        live = _snapshot()
        assert live[0] and live[1]

        rebuild_player_ratings(chunk_size=7)

        assert _snapshot() == live

    def test_rebuilding_twice_is_idempotent(self):
        _history()

        rebuild_player_ratings()
        first = _snapshot()
        rebuild_player_ratings()

        assert _snapshot() == first

    def test_a_deleted_player_takes_their_rows_with_them(self):
        _a, b, _c, _d = _history()
        b.delete()

        rebuild_player_ratings()

        assert not PlayerRating.all_objects.filter(player_id=b.pk).exists()

    def test_frozen_elo_columns_survive_a_rebuild(self):
        """Pre-KAN-73 rows carry Elo history in `RatingChange.elo_*`; a rebuild
        deletes and re-creates the rows, and must carry those values onto the
        rebuilt row for the same match, player, season and format."""
        _history()
        frozen = {}
        for rc in RatingChange.objects.select_related("rating").order_by("pk"):
            values = {
                "elo_old_rating": 1400 + rc.pk,
                "elo_new_rating": 1410 + rc.pk,
                "elo_change": 10 + rc.pk % 7,
                "elo_k_factor": 32.0 + rc.pk % 3,
            }
            RatingChange.objects.filter(pk=rc.pk).update(**values)
            key = (rc.match_id, rc.player_id, rc.rating.season_id, rc.rating.format)
            frozen[key] = tuple(values.values())
        assert any(k[2] is not None for k in frozen)  # season rows are covered
        assert any(k[3] == "doubles" for k in frozen)
        old_pks = set(RatingChange.objects.values_list("pk", flat=True))

        rebuild_player_ratings(chunk_size=7)

        rebuilt = {
            (match_id, player_id, season_id, fmt): tuple(values)
            for match_id, player_id, season_id, fmt, *values in RatingChange.objects.values_list(
                "match_id", "player_id", "rating__season_id", "rating__format",
                *ELO_HISTORY_FIELDS,
            )
        }
        assert not old_pks & set(RatingChange.objects.values_list("pk", flat=True))  # re-created
        assert rebuilt == frozen

    def test_rows_without_elo_history_stay_null_after_a_rebuild(self):
        _history()

        rebuild_player_ratings()

        assert RatingChange.objects.exists()
        assert not RatingChange.objects.filter(elo_old_rating__isnull=False).exists()


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
    def test_the_rating_tables_reverse_and_a_rebuild_restores_them(self, at_leaf):
        _history()
        live = _snapshot()

        _migrate_to("0038_season")
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
        assert "pingpong_playerrating" not in tables
        assert "pingpong_ratingchange" not in tables
        assert Player.objects.count() == 4

        _migrate_to(_leaf())
        # 0040 is a frozen no-op: reapplying the chain leaves the tables empty,
        # and a rebuild (what `recalculate_ratings` runs) restores them.
        assert not PlayerRating.all_objects.exists()
        rebuild_player_ratings()
        assert _snapshot() == live

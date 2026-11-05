"""Doubles under Glicko-2 (KAN-70, docs/decisions/kan-63-glicko2.md "Doubles").

Each player is rated in a one-game period against a composite opponent (mean
rating, RMS of the RDs) with their own RD. The side-combination policy is a pure
helper tested without a database; the rest pins the live write, the preview, the
race guards and the replay.
"""

import logging
import math
from datetime import date
from unittest import mock

import pytest
from django.core.management import call_command

from .. import glicko2, glicko_adapter, rating_pipeline
from ..glicko2 import Rating
from ..glicko_adapter import DEFAULT_PARAMS, composite_opponent
from ..models import EloHistory, Match, PlayerRating, RatingChange, Side
from ..rating_pipeline import LiveBook, RatingScope, apply_match_ratings
from ..rating_replay import MemoryBook, ReplayMatch, ReplayPlayer
from .conftest import (
    GameFactory,
    MatchFactory,
    PlayerFactory,
    PlayerRatingFactory,
    confirm_match_silent,
)
from .test_glicko_read import _at, _finish

SINGLES = RatingScope(None, "singles")
DOUBLES = RatingScope(None, "doubles")
DAY = _at(2026, 2, 1)
DAY_DATE = date(2026, 2, 1)
FIELDS = glicko_adapter.SHADOW_FIELDS


def _row(player, season=None, fmt="singles"):
    return PlayerRating.all_objects.get(player=player, season=season, format=fmt)


def _state(row):
    return tuple(getattr(row, f) for f in FIELDS)


def _seed(player, rd, rating=1500.0, fmt="doubles"):
    """A rated row on the match day, so nothing inflates."""
    return PlayerRatingFactory(
        player=player,
        format=fmt,
        matches=5,
        glicko_rating=rating,
        rd=rd,
        volatility=0.06,
        last_rated_on=DAY_DATE,
    )


def _pending_doubles(winner=Side.ONE):
    """A won, confirmed doubles match whose ratings have NOT been applied yet."""
    players = [PlayerFactory(with_user=True) for _ in range(4)]
    for p in players:
        p.user.profile.email_verified = True
        p.user.profile.save()
    match = MatchFactory(
        team1_players=players[:2],
        team2_players=players[2:],
        is_double=True,
        best_of=5,
        date_played=DAY,
    )
    score = (11, 5) if winner == Side.ONE else (5, 11)
    for n in range(1, 4):
        GameFactory(
            match=match, game_number=n, team1_score=score[0], team2_score=score[1]
        )
    match.refresh_from_db()
    confirm_match_silent(match)
    match.invalidate_confirmation_state()
    return match, players


def _project(match, scope=DOUBLES):
    return glicko_adapter.projected_glicko_changes(
        Match.all_objects.get(pk=match.pk), scope
    )


def _by_player(projection):
    return {u.player_id: u for u in projection.updates}


class TestCompositeOpponent:
    """The pure side-combination policy: no database."""

    def test_the_rating_is_the_mean_and_the_rd_is_the_rms(self):
        rating, rd = composite_opponent(
            [Rating(1600, 60, 0.06), Rating(1400, 350, 0.06)]
        )

        assert rating == 1500
        assert rd == pytest.approx(math.sqrt((60**2 + 350**2) / 2))

    def test_one_uncertain_partner_raises_the_pair_above_the_plain_mean(self):
        _, rd = composite_opponent([Rating(1500, 60, 0.06), Rating(1500, 350, 0.06)])

        assert rd > (60 + 350) / 2
        assert rd < 350

    def test_equal_rds_give_that_rd(self):
        _, rd = composite_opponent([Rating(1500, 80, 0.06)] * 2)

        assert rd == pytest.approx(80)

    def test_a_side_of_one_is_that_player(self):
        assert composite_opponent([Rating(1623.5, 71, 0.06)]) == (
            1623.5,
            pytest.approx(71),
        )

    def test_any_number_of_players(self):
        rating, rd = composite_opponent(
            [Rating(r, 100, 0.06) for r in (1400, 1500, 1700)]
        )

        assert rating == pytest.approx(1533.3333333)
        assert rd == pytest.approx(100)

    def test_it_accepts_a_generator_and_is_order_independent(self):
        a, b = Rating(1450, 50, 0.06), Rating(1650, 200, 0.06)

        assert composite_opponent(x for x in (a, b)) == composite_opponent([b, a])

    def test_an_empty_side_is_an_error(self):
        with pytest.raises(ValueError):
            composite_opponent([])


@pytest.mark.django_db
class TestPerPlayerUpdate:
    def test_a_new_partner_moves_much_more_than_an_established_one(self):
        match, (a, b, c, d) = _pending_doubles()
        _seed(a, rd=60)  # b is brand new: no row, RD 350
        _seed(c, rd=60)
        _seed(d, rd=60)

        updates = _by_player(_project(match))

        established, newcomer = updates[a.pk], updates[b.pk]
        gain_established = established.new.rating - established.old.rating
        gain_newcomer = newcomer.new.rating - newcomer.old.rating
        assert gain_established > 0
        assert gain_newcomer > 5 * gain_established
        assert (newcomer.old.rd - newcomer.new.rd) > 5 * (
            established.old.rd - established.new.rd
        )

    def test_each_player_uses_their_own_phi_against_the_same_opponent(self):
        match, (a, b, c, d) = _pending_doubles()
        _seed(a, rd=60)
        _seed(c, rd=100, rating=1560.0)
        _seed(d, rd=300, rating=1640.0)

        updates = _by_player(_project(match))

        opp_rating, opp_rd = composite_opponent(
            [updates[c.pk].inflated, updates[d.pk].inflated]
        )
        assert opp_rating == pytest.approx(1600)
        assert opp_rd == pytest.approx(math.sqrt((100**2 + 300**2) / 2))
        for player in (a, b):
            u = updates[player.pk]
            assert u.new == glicko2.rate(
                u.inflated,
                [glicko2.Game(opp_rating, opp_rd, 1.0)],
                DEFAULT_PARAMS.tau,
                rd_max=DEFAULT_PARAMS.rd_max,
            )
        # And the losers are rated against the winners' composite, with score 0.
        w_rating, w_rd = composite_opponent(
            [updates[a.pk].inflated, updates[b.pk].inflated]
        )
        assert updates[d.pk].new == glicko2.rate(
            updates[d.pk].inflated,
            [glicko2.Game(w_rating, w_rd, 0.0)],
            DEFAULT_PARAMS.tau,
            rd_max=DEFAULT_PARAMS.rd_max,
        )

    def test_both_winners_gain_against_a_stronger_pair_and_losers_lose(self):
        match, (a, b, c, d) = _pending_doubles()
        for p in (a, b):
            _seed(p, rd=80, rating=1450.0)
        for p in (c, d):
            _seed(p, rd=80, rating=1650.0)

        updates = _by_player(_project(match))

        assert updates[a.pk].new.rating > updates[a.pk].old.rating
        assert updates[b.pk].new.rating > updates[b.pk].old.rating
        assert updates[c.pk].new.rating < updates[c.pk].old.rating
        assert updates[d.pk].new.rating < updates[d.pk].old.rating

    def test_the_gain_reflects_the_opponents_combined_uncertainty(self):
        def winner_gain(opp_rds):
            match, (a, b, c, d) = _pending_doubles()
            for p in (a, b):
                _seed(p, rd=80, rating=1450.0)
            for p, rd in zip((c, d), opp_rds):
                _seed(p, rd=rd, rating=1650.0)
            u = _by_player(_project(match))[a.pk]
            return u.new.rating - u.old.rating

        certain = winner_gain((60, 60))
        mixed = winner_gain((60, 350))
        plain_mean_equivalent = winner_gain((205, 205))  # the mean of 60 and 350
        rms_equivalent = winner_gain((math.sqrt((60**2 + 350**2) / 2),) * 2)

        assert mixed != pytest.approx(certain)
        assert mixed != pytest.approx(plain_mean_equivalent)
        assert mixed == pytest.approx(rms_equivalent)

    def test_the_side_change_is_the_mean_delta_of_the_sides_players(self):
        match, (a, b, c, d) = _pending_doubles()
        _seed(a, rd=60)
        projection = _project(match)
        updates = _by_player(projection)

        mean1 = (
            sum(updates[p.pk].new.rating - updates[p.pk].old.rating for p in (a, b)) / 2
        )
        mean2 = (
            sum(updates[p.pk].new.rating - updates[p.pk].old.rating for p in (c, d)) / 2
        )
        assert projection.side1_change == round(mean1)
        assert projection.side2_change == round(mean2)
        assert len(projection.updates) == 4
        assert projection.is_finite()


@pytest.mark.django_db
class TestOnlyTheDoublesRowsChange:
    def test_a_doubles_match_writes_four_doubles_rows_and_no_singles_row(self):
        players = [PlayerFactory() for _ in range(4)]
        singles_before = {
            p.pk: _seed(p, rd=70, rating=1520.0, fmt="singles") for p in players
        }
        before = {p.pk: _state(_row(p, fmt="singles")) for p in players}

        match = _finish(players[:2], players[2:], when=DAY)

        assert RatingChange.objects.filter(match=match).count() == 4
        assert not EloHistory.objects.filter(match=match).exists()  # frozen, never written
        for p in players:
            doubles = _row(p, fmt="doubles")
            assert all(getattr(doubles, f) is not None for f in FIELDS)
            assert doubles.last_rated_on == DAY_DATE
            assert _state(_row(p, fmt="singles")) == before[p.pk]
            assert _row(p, fmt="singles").matches == singles_before[p.pk].matches
        assert _row(players[0], fmt="doubles").glicko_rating > 1500
        assert _row(players[2], fmt="doubles").glicko_rating < 1500

    def test_a_singles_match_leaves_the_doubles_rows_alone(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        _seed(p1, rd=70, rating=1520.0)
        _seed(p2, rd=70, rating=1480.0)
        before = (_state(_row(p1, fmt="doubles")), _state(_row(p2, fmt="doubles")))

        _finish([p1], [p2], when=DAY)

        assert (
            _state(_row(p1, fmt="doubles")),
            _state(_row(p2, fmt="doubles")),
        ) == before
        assert _row(p1, fmt="singles").glicko_rating is not None


@pytest.mark.django_db
class TestIdempotencyAndRaces:
    def _snapshot(self, players):
        return [_state(_row(p, fmt="doubles")) for p in players]

    def test_four_confirmations_write_once(self):
        match, players = _pending_doubles()

        apply_match_ratings(match)
        once = self._snapshot(players)
        for _ in range(3):  # the other three confirmers arrive
            apply_match_ratings(match)

        assert self._snapshot(players) == once
        assert all(_row(p, fmt="doubles").matches == 1 for p in players)
        assert RatingChange.objects.filter(match=match).count() == 4

    def test_a_confirmer_that_loses_the_race_writes_the_shadow_once(self):
        match, players = _pending_doubles()
        apply_match_ratings(match)
        once = self._snapshot(players)

        with mock.patch.object(
            rating_pipeline, "already_applied", side_effect=[False, True]
        ):
            apply_match_ratings(match)

        assert self._snapshot(players) == once

    def test_a_retried_write_after_an_integrity_error_applies_once(self):
        match, players = _pending_doubles()
        real, calls = rating_pipeline._apply_match, []

        def flaky(m):
            calls.append(1)
            if len(calls) == 1:
                raise rating_pipeline.IntegrityError("conflict")
            return real(m)

        with mock.patch.object(rating_pipeline, "_apply_match", side_effect=flaky):
            apply_match_ratings(match)

        assert len(calls) == 2
        assert all(_row(p, fmt="doubles").glicko_rating > 1500 for p in players[:2])
        assert all(_row(p, fmt="doubles").matches == 1 for p in players)
        assert RatingChange.objects.filter(match=match).count() == 4

    def test_a_flush_that_fails_after_the_rows_mutated_is_a_single_application(self):
        plain, plain_players = _pending_doubles()
        apply_match_ratings(plain)
        baseline = self._snapshot(plain_players)

        retried, players = _pending_doubles()
        real_flush, calls = LiveBook.flush, []

        def flaky_flush(self, match, changes):
            calls.append(1)
            if len(calls) == 1:
                raise rating_pipeline.IntegrityError("conflict after the rows were mutated")
            return real_flush(self, match, changes)

        with mock.patch.object(LiveBook, "flush", flaky_flush):
            apply_match_ratings(retried)

        assert len(calls) == 2
        assert self._snapshot(players) == baseline


@pytest.mark.django_db
class TestPreviewIsThePersistedValue:
    def test_projection_before_confirming_equals_what_is_written(self):
        match, players = _pending_doubles()
        _seed(players[0], rd=60)
        projection = _project(match)

        apply_match_ratings(match)

        updates = _by_player(projection)
        for p in players:
            row, update = _row(p, fmt="doubles"), updates[p.pk]
            assert row.glicko_rating == update.new.rating
            assert row.rd == update.new.rd
            assert row.volatility == update.new.volatility

    def test_the_guarded_projection_runs_no_queries(self, django_assert_num_queries):
        match, _players = _pending_doubles()
        book = LiveBook(match)
        match.sides()

        with django_assert_num_queries(0):
            assert (
                rating_pipeline.guarded_projection(match, DOUBLES, book, DEFAULT_PARAMS)
                is not None
            )


def _stub(side1, side2, *, is_double=True):
    """A replay-shaped match, for shapes the database will not hold."""
    record = ReplayMatch(
        pk=1,
        date_played=DAY_DATE,
        created_at=None,
        match_type="casual",
        best_of=5,
        is_double=is_double,
        winner_side=Side.ONE,
        club_id=None,
        side1_ids=tuple(side1),
        side2_ids=tuple(side2),
    )
    record._players = (
        [ReplayPlayer(pk) for pk in side1],
        [ReplayPlayer(pk) for pk in side2],
    )
    return record


class TestOddShapes:
    """Pure projection over an in-memory book: no database."""

    def test_a_one_versus_two_side_rates_without_crashing(self):
        projection = glicko_adapter.project(
            _stub([1], [2, 3]), DOUBLES, MemoryBook({}), DEFAULT_PARAMS
        )

        assert {u.player_id for u in projection.updates} == {1, 2, 3}
        assert projection.is_finite()
        assert projection.side1_change > 0 > projection.side2_change

    def test_three_a_side_is_not_hard_coded_to_two(self):
        projection = glicko_adapter.project(
            _stub([1, 2, 3], [4, 5, 6]), DOUBLES, MemoryBook({}), DEFAULT_PARAMS
        )

        assert len(projection.updates) == 6

    def test_the_same_player_on_both_sides_is_skipped_and_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger=glicko_adapter.logger.name):
            projection = glicko_adapter.project(
                _stub([1, 2], [2, 3]), DOUBLES, MemoryBook({}), DEFAULT_PARAMS
            )

        assert projection is None
        assert "appears more than once" in caplog.text

    def test_the_same_player_twice_on_one_side_is_skipped_too(self, caplog):
        with caplog.at_level(logging.WARNING, logger=glicko_adapter.logger.name):
            projection = glicko_adapter.project(
                _stub([1, 1], [3, 4]), DOUBLES, MemoryBook({}), DEFAULT_PARAMS
            )

        assert projection is None
        assert caplog.text

    def test_an_empty_side_keeps_the_guard(self):
        for sides in (([], [3, 4]), ([1, 2], []), ([], [])):
            assert (
                glicko_adapter.project(
                    _stub(*sides), DOUBLES, MemoryBook({}), DEFAULT_PARAMS
                )
                is None
            )

    def test_a_match_with_no_winner_projects_to_none(self):
        record = _stub([1, 2], [3, 4])
        record.winner_side = None

        assert (
            glicko_adapter.project(record, DOUBLES, MemoryBook({}), DEFAULT_PARAMS)
            is None
        )


@pytest.mark.django_db
class TestOddShapesThroughTheDatabase:
    def test_a_one_versus_two_doubles_match_confirms_with_a_shadow(self):
        a, b, c = PlayerFactory(), PlayerFactory(), PlayerFactory()
        match = MatchFactory(
            team1_players=[a],
            team2_players=[b, c],
            is_double=True,
            best_of=5,
            date_played=DAY,
        )
        for n in range(1, 4):
            GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)

        for p in (a, b, c):
            assert _row(p, fmt="doubles").glicko_rating is not None
        assert _row(a, fmt="doubles").glicko_rating > 1500

    def test_a_side_emptied_by_a_deleted_player_keeps_the_guard(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = MatchFactory(
            team1_players=[p1], team2_players=[p2], best_of=5, date_played=DAY
        )
        for n in range(1, 4):
            GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)
        p2.delete()
        match = Match.all_objects.get(pk=match.pk)

        assert not match.sides()[1]
        assert glicko_adapter.projected_glicko_changes(match, SINGLES) is None


@pytest.mark.django_db
class TestReplayMatchesLive:
    def test_recalculate_ratings_rebuilds_the_same_doubles_ratings(self):
        a, b, c, d, e = (PlayerFactory() for _ in range(5))
        history = [
            ([a, b], [c, d], _at(2026, 1, 3)),
            ([a, c], [b, e], _at(2026, 1, 3)),
            ([d, e], [a, b], _at(2026, 1, 20)),
            ([c, e], [a, d], _at(2026, 4, 2)),
        ]
        for side1, side2, when in history:
            _finish(side1, side2, when=when)

        def snapshot():
            return {
                (r.player_id, r.season_id, r.format): (r.matches, *_state(r))
                for r in PlayerRating.all_objects.filter(format="doubles")
            }

        live = snapshot()
        assert len(live) == 5
        assert all(v[1] is not None for v in live.values())

        call_command("recalculate_ratings")
        replayed = snapshot()

        assert replayed.keys() == live.keys()
        for key, expected in live.items():
            got = replayed[key]
            assert got[0] == expected[0]
            assert got[1:4] == pytest.approx(expected[1:4])
            assert got[4] == expected[4]

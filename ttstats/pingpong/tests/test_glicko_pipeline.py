"""The rating write path: `rating_pipeline.apply_match_ratings` (KAN-73).

Formerly `test_elo.py`. Elo arithmetic (K-factor, expected score, exact Elo
deltas, the legacy Player fields, EloHistory writes) is gone with Elo; the
structural promises of the write path are kept here against Glicko-2:
guards, idempotency, race safety, the signal path, fresh locked reads, format
and season scoping, and that the replay reproduces the live state.
"""

import re
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import IntegrityError

from .. import glicko_adapter, rating_pipeline, ratings
from ..models import EloHistory, Match, Player, PlayerRating, RatingChange, Side
from ..rating_pipeline import RatingScope, apply_match_ratings
from .conftest import (
    ClubFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    PlayerRatingFactory,
    SeasonFactory,
    confirm_match,
    confirm_match_silent,
)

SINGLES = RatingScope(None, "singles")
GLICKO_STATE = ("glicko_rating", "rd", "volatility", "last_rated_on")
ELO_COLUMNS = ("elo_old_rating", "elo_new_rating", "elo_change", "elo_k_factor")


def _at(year, month, day):
    return datetime(year, month, day, 12, tzinfo=UTC)


def _finish(side1, side2, *, winner=Side.ONE, club=None, when=None, best_of=5, **kwargs):
    """A won match. Players without accounts auto-confirm, so ratings are applied."""
    extra = {"club": club} if club is not None else {}
    if when is not None:
        extra["date_played"] = when
    match = MatchFactory(
        team1_players=list(side1), team2_players=list(side2),
        is_double=len(side1) > 1, best_of=best_of, **extra, **kwargs,
    )
    for n in range(1, best_of // 2 + 2):
        t1, t2 = (11, 5) if winner == Side.ONE else (5, 11)
        GameFactory(match=match, game_number=n, team1_score=t1, team2_score=t2)
    match.refresh_from_db()
    return match


def _all_time(player, fmt="singles"):
    return PlayerRating.all_objects.filter(player=player, season=None, format=fmt).first()


def _state(row):
    return tuple(getattr(row, f) for f in GLICKO_STATE)


def _verified_pair(**kwargs):
    players = [PlayerFactory(with_user=True, **kwargs) for _ in range(2)]
    for p in players:
        p.user.profile.email_verified = True
        p.user.profile.save()
    return players


def _pending_confirmed(when=None):
    """A won, confirmed match whose ratings have NOT been applied yet."""
    p1, p2 = _verified_pair()
    match = _finish([p1], [p2], when=when)  # verified players: unconfirmed, nothing applied
    confirm_match_silent(match)  # confirmed without firing the pipeline
    match.invalidate_confirmation_state()
    return match, p1, p2


def _rated(player, rating, *, rd=80.0, matches=30, on=date(2026, 2, 1), **kwargs):
    return PlayerRatingFactory(
        player=player, matches=matches, glicko_rating=float(rating), rd=rd,
        volatility=0.06, last_rated_on=on, **kwargs,
    )


def _fresh_projection(match, scope=SINGLES):
    return glicko_adapter.projected_glicko_changes(Match.all_objects.get(pk=match.pk), scope)


@pytest.mark.django_db
class TestGuards:
    def test_no_update_without_winner(self):
        match = MatchFactory(confirmed=True)  # no games, so no winner

        apply_match_ratings(match)

        assert not PlayerRating.all_objects.exists()
        assert not RatingChange.objects.exists()

    def test_no_update_without_confirmation(self):
        p1, p2 = _verified_pair()
        match = _finish([p1], [p2])  # verified players: a winner, not confirmed
        assert match.winner_side == Side.ONE

        apply_match_ratings(match)

        assert not PlayerRating.all_objects.exists()
        assert not RatingChange.objects.exists()

    def test_singles_match_with_an_empty_side_is_skipped_not_crashed(self):
        """The empty-side guard used to run only for doubles, so a singles
        match with no players on a side raised IndexError."""
        match, _p1, _p2 = _pending_confirmed()
        match.participants.filter(side=Side.ONE).delete()
        match.save()
        match.refresh_from_db()
        assert list(match.side1_players) == []

        apply_match_ratings(match)  # must not raise

        assert not RatingChange.objects.filter(match=match).exists()
        assert not PlayerRating.all_objects.exists()


@pytest.mark.django_db
class TestTheSignalPathAppliesRatings:
    def test_completion_with_unverified_players_applies_ratings(self):
        p1, p2 = PlayerFactory(), PlayerFactory()

        match = _finish([p1], [p2])

        assert match.is_confirmed
        assert RatingChange.objects.filter(match=match).count() == 2
        assert _all_time(p1).matches == 1

    def test_ratings_wait_for_the_last_verified_confirmation(self):
        p1, p2 = _verified_pair()
        match = _finish([p1], [p2])

        confirm_match(match, [p1])
        assert not RatingChange.objects.filter(match=match).exists()

        confirm_match(match, [p2])
        assert RatingChange.objects.filter(match=match).count() == 2
        assert _all_time(p1).glicko_rating > 1500 > _all_time(p2).glicko_rating


@pytest.mark.django_db
class TestGlickoWrite:
    def test_winner_gains_and_loser_loses_symmetrically(self):
        match, p1, p2 = _pending_confirmed()

        apply_match_ratings(match)

        r1, r2 = _all_time(p1), _all_time(p2)
        assert r1.glicko_rating > 1500 > r2.glicko_rating
        # Two unrated players at the same RD: the movement is symmetric.
        assert r1.glicko_rating + r2.glicko_rating == pytest.approx(3000)
        assert r1.rd < 350 and r2.rd < 350

    def test_an_upset_moves_more_than_an_expected_win(self):
        day = _at(2026, 2, 1)
        underdog, favorite = PlayerFactory(), PlayerFactory()
        fav2, dog2 = PlayerFactory(), PlayerFactory()
        for p, r in ((underdog, 1400), (favorite, 1600), (fav2, 1600), (dog2, 1400)):
            _rated(p, r)

        _finish([underdog], [favorite], when=day)
        _finish([fav2], [dog2], when=day)

        upset_gain = _all_time(underdog).glicko_rating - 1400
        expected_gain = _all_time(fav2).glicko_rating - 1600
        assert upset_gain > expected_gain > 0
        assert 1600 - _all_time(favorite).glicko_rating > 1400 - _all_time(dog2).glicko_rating > 0

    def test_rating_change_rows_hold_the_glicko_history(self):
        match, p1, p2 = _pending_confirmed()

        apply_match_ratings(match)

        for player in (p1, p2):
            rc = RatingChange.objects.get(match=match, player=player)
            row = _all_time(player)
            assert rc.rating_id == row.pk
            assert rc.glicko_old_rating == 1500
            assert rc.glicko_new_rating == row.glicko_rating
            assert rc.glicko_change == round(row.glicko_rating - 1500)
            assert rc.glicko_rd_before == 350
            assert rc.glicko_rd_after == row.rd

    def test_writes_no_elo_history_and_leaves_the_elo_columns_null(self):
        """EloHistory and RatingChange.elo_* are frozen pre-KAN-73 history."""
        club = ClubFactory()
        SeasonFactory(club=club, starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))
        a, b, c, d = (PlayerFactory(club=club) for _ in range(4))
        _finish([a], [b], club=club, when=_at(2026, 2, 1))
        _finish([a, b], [c, d], club=club, when=_at(2026, 2, 2))
        match, _p1, _p2 = _pending_confirmed()
        apply_match_ratings(match)

        assert RatingChange.objects.count() == 2 * 2 + 4 * 2 + 2
        assert not EloHistory.objects.exists()
        assert not RatingChange.objects.exclude(
            elo_old_rating=None, elo_new_rating=None, elo_change=None, elo_k_factor=None
        ).exists()

    def test_matches_are_counted_on_top_of_the_stored_count(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        _rated(p1, 1500, matches=10)
        _rated(p2, 1500, matches=20)

        _finish([p1], [p2], when=_at(2026, 2, 1))

        assert (_all_time(p1).matches, _all_time(p2).matches) == (11, 21)

    def test_calling_twice_writes_ratings_once(self):
        match, p1, _p2 = _pending_confirmed()

        apply_match_ratings(match)
        once = _state(_all_time(p1))
        apply_match_ratings(match)

        assert _state(_all_time(p1)) == once
        assert _all_time(p1).matches == 1
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_already_applied_is_true_once_rating_changes_exist(self):
        match, _p1, _p2 = _pending_confirmed()
        assert not rating_pipeline.already_applied(match)

        apply_match_ratings(match)

        assert rating_pipeline.already_applied(match)

    def test_a_match_rated_under_elo_without_rating_rows_is_not_rated_again(self):
        """Frozen EloHistory alone counts as applied: an Elo-era match that never
        got RatingChange rows must not be rated on top of today's ratings."""
        match, p1, p2 = _pending_confirmed()
        for p, old, new in ((p1, 1500, 1516), (p2, 1500, 1484)):
            EloHistory.objects.create(
                match=match, player=p, old_rating=old, new_rating=new,
                rating_change=new - old, k_factor=32.0,
            )

        assert rating_pipeline.already_applied(match)
        apply_match_ratings(match)

        assert not RatingChange.objects.filter(match=match).exists()
        assert not PlayerRating.objects.filter(player__in=[p1, p2]).exists()


@pytest.mark.django_db
class TestFormatRatingsAreSeparate:
    def test_a_singles_match_changes_only_singles(self):
        p1, p2 = PlayerFactory(), PlayerFactory()

        match = _finish([p1], [p2])

        assert not PlayerRating.all_objects.filter(format="doubles").exists()
        r1 = _all_time(p1)
        rc1 = RatingChange.objects.get(match=match, player=p1)
        assert (r1.glicko_rating, r1.matches) == (rc1.glicko_new_rating, 1)
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_a_doubles_match_changes_only_doubles_per_player(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))

        match = _finish([a, b], [c, d])

        assert not PlayerRating.all_objects.filter(format="singles").exists()
        changes = {rc.player_id: rc for rc in RatingChange.objects.filter(match=match)}
        assert len(changes) == 4
        # Everyone started level, so partners move alike; each has their own row.
        assert changes[a.pk].glicko_change == changes[b.pk].glicko_change > 0
        assert changes[c.pk].glicko_change == changes[d.pk].glicko_change < 0
        assert len({rc.rating_id for rc in changes.values()}) == 4

    def test_a_doubles_only_player_is_unrated_in_singles(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        _finish([a, b], [c, d])

        assert _all_time(a, "singles") is None
        assert _all_time(a, "doubles") is not None

    def test_each_format_counts_its_own_matches(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        _finish([a], [b])
        _finish([a, c], [b, d])

        assert _all_time(a, "singles").matches == 1
        assert _all_time(a, "doubles").matches == 1

    def test_the_preview_for_a_scope_is_what_gets_written(self):
        match, p1, p2 = _pending_confirmed(when=_at(2026, 2, 1))
        _rated(p1, 1620)
        preview, pending = ratings.projected_changes_by_player(
            Match.all_objects.get(pk=match.pk), SINGLES
        )
        assert pending

        apply_match_ratings(match)

        written = {rc.player_id: rc for rc in RatingChange.objects.filter(match=match)}
        assert written[p1.pk].glicko_change == preview[p1.pk]
        assert written[p2.pk].glicko_change == preview[p2.pk]
        assert written[p1.pk].glicko_old_rating == 1620


@pytest.mark.django_db
class TestSeasonRatings:
    def _season_q1(self, club):
        return SeasonFactory(club=club, starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31))

    def test_a_match_in_a_season_writes_all_time_and_season_rows(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        match = _finish([p1], [p2], club=club, when=_at(2026, 2, 1))

        rows = PlayerRating.all_objects.filter(player=p1)
        assert {(r.season_id, r.format) for r in rows} == {(None, "singles"), (season.pk, "singles")}
        season_row = rows.get(season=season)
        assert season_row.club_id == club.pk
        assert season_row.matches == 1
        assert season_row.glicko_rating is not None
        assert RatingChange.objects.filter(match=match).count() == 4

    def test_first_season_rating_is_seeded_from_the_all_time_state_before_this_match(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)
        _rated(p1, 1700, rd=60.0, matches=40)

        match = _finish([p1], [p2], club=club, when=_at(2026, 2, 1))

        season_change = RatingChange.objects.get(match=match, player=p1, rating__season=season)
        all_time_change = RatingChange.objects.get(match=match, player=p1, rating__season=None)
        assert all_time_change.glicko_old_rating == 1700
        assert season_change.glicko_old_rating == 1600  # 1500 + 0.5 * (1700 - 1500)
        assert season_change.glicko_rd_before == glicko_adapter.DEFAULT_PARAMS.season_rd_floor
        season_row = PlayerRating.all_objects.get(player=p1, season=season)
        assert season_row.glicko_rating == season_change.glicko_new_rating
        assert season_row.matches == 1

    def test_an_unrated_player_starts_the_season_at_1500(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        match = _finish([p1], [p2], club=club, when=_at(2026, 2, 1))

        change = RatingChange.objects.get(match=match, player=p2, rating__season=season)
        assert change.glicko_old_rating == 1500

    def test_a_second_season_match_continues_the_season_row(self):
        club = ClubFactory()
        season = self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)
        _finish([p1], [p2], club=club, when=_at(2026, 2, 1))
        after_first = PlayerRating.all_objects.get(player=p1, season=season).glicko_rating

        second = _finish([p1], [p2], club=club, when=_at(2026, 2, 2))

        change = RatingChange.objects.get(match=second, player=p1, rating__season=season)
        assert change.glicko_old_rating == after_first
        assert PlayerRating.all_objects.get(player=p1, season=season).matches == 2

    def test_a_match_outside_every_season_counts_all_time_only(self):
        club = ClubFactory()
        self._season_q1(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        match = _finish([p1], [p2], club=club, when=_at(2026, 4, 1))

        assert not PlayerRating.all_objects.filter(season__isnull=False).exists()
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_another_clubs_season_does_not_apply(self):
        self._season_q1(ClubFactory())
        club = ClubFactory()
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        _finish([p1], [p2], club=club, when=_at(2026, 2, 1))

        assert not PlayerRating.all_objects.filter(season__isnull=False).exists()


@pytest.mark.django_db
class TestRatingWritesAreRaceSafe:
    def test_a_confirmer_that_loses_the_race_writes_nothing_and_does_not_raise(self):
        """Both confirmers pass the 'already applied?' guard; the second one's
        inserts hit the unique constraint, the whole block rolls back, and it
        treats that as already applied."""
        match, p1, p2 = _pending_confirmed()
        apply_match_ratings(match)
        before = (_state(_all_time(p1)), _state(_all_time(p2)),
                  _all_time(p1).matches, RatingChange.objects.count())

        # Guard says "not yet" (we raced), the post-failure re-check says "done".
        with mock.patch.object(rating_pipeline, "already_applied", side_effect=[False, True]):
            apply_match_ratings(match)

        assert (_state(_all_time(p1)), _state(_all_time(p2)),
                _all_time(p1).matches, RatingChange.objects.count()) == before

    def test_a_conflict_that_is_not_this_match_is_retried(self):
        """Two *different* matches creating the same new rating row: the loser
        must retry, not mistake it for having already applied."""
        match, _p1, _p2 = _pending_confirmed()
        real_apply = rating_pipeline._apply_match
        calls = []

        def flaky(m):
            calls.append(m.pk)
            if len(calls) == 1:
                raise IntegrityError("unique constraint on a fresh all-time row")
            return real_apply(m)

        with mock.patch.object(rating_pipeline, "_apply_match", side_effect=flaky):
            apply_match_ratings(match)

        assert len(calls) == 2
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_a_second_conflict_is_raised(self):
        match, _p1, _p2 = _pending_confirmed()

        with mock.patch.object(
            rating_pipeline, "_apply_match", side_effect=IntegrityError("again")
        ), pytest.raises(IntegrityError):
            apply_match_ratings(match)

        assert not RatingChange.objects.filter(match=match).exists()


@pytest.mark.django_db
class TestRetriedWriteAppliesOnce:
    """A retry after a rolled-back attempt must start from the database, not
    from rating rows the failed attempt already mutated in memory."""

    def test_a_flush_conflict_on_the_first_attempt_applies_the_match_once(self):
        match, p1, p2 = _pending_confirmed(when=_at(2026, 2, 1))
        _rated(p1, 1560, matches=12)
        _rated(p2, 1480, matches=7)
        preview = {u.player_id: u for u in _fresh_projection(match).updates}
        match.sides()  # the memo a real signal path would already hold
        real_flush = rating_pipeline.LiveBook.flush
        attempts = []

        def conflict_once(self, m, changes):
            attempts.append(m.pk)
            if len(attempts) == 1:
                assert changes[0].row.glicko_rating != 1560  # rows already mutated
                raise IntegrityError("another match created the same new rating row")
            return real_flush(self, m, changes)

        with mock.patch.object(rating_pipeline.LiveBook, "flush", conflict_once):
            apply_match_ratings(match)

        assert len(attempts) == 2
        for player, matches in ((p1, 13), (p2, 8)):
            row = _all_time(player)
            assert row.glicko_rating == pytest.approx(preview[player.pk].new.rating)
            assert row.rd == pytest.approx(preview[player.pk].new.rd)
            assert row.matches == matches
        rc1 = RatingChange.objects.get(match=match, player=p1)
        assert rc1.glicko_old_rating == 1560
        assert RatingChange.objects.filter(match=match).count() == 2

    def test_a_lost_race_leaves_the_match_memo_matching_the_database(self):
        match, p1, _ = _pending_confirmed()
        apply_match_ratings(match)
        match.sides()  # memo loaded with the old name
        Player.objects.filter(pk=p1.pk).update(name="Renamed")

        with mock.patch.object(rating_pipeline, "already_applied", side_effect=[False, True]):
            apply_match_ratings(match)

        assert match.sides()[0][0].name == "Renamed"


@pytest.mark.django_db
class TestReadsLockedFreshRows:
    def test_a_stale_sides_memo_is_not_what_gets_written(self):
        """Another match for P committed after this match's memo was loaded:
        the write must build on P's current row, not overwrite it."""
        match, p1, _p2 = _pending_confirmed(when=_at(2026, 2, 1))
        match.sides()  # memo loaded while P is unrated
        _rated(p1, 1700, matches=30)
        preview = {u.player_id: u for u in _fresh_projection(match).updates}

        apply_match_ratings(match)

        rc1 = RatingChange.objects.get(match=match, player=p1)
        assert rc1.glicko_old_rating == 1700
        assert rc1.glicko_new_rating == pytest.approx(preview[p1.pk].new.rating)
        row = _all_time(p1)
        assert row.glicko_rating == pytest.approx(preview[p1.pk].new.rating)
        assert row.matches == 31

    def test_lock_sides_rebuilds_the_sides_in_participant_order(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        # Higher pks first on each side, so pk order differs from participant order.
        match = MatchFactory(team1_players=[d, a], team2_players=[c, b], is_double=True)

        from django.db import transaction

        with transaction.atomic():
            side1, side2 = rating_pipeline._lock_sides(match)

        expected = match.sides()
        assert [p.pk for p in side1] == [p.pk for p in expected[0]]
        assert [p.pk for p in side2] == [p.pk for p in expected[1]]
        assert {p.pk for p in side1} == {a.pk, d.pk}
        assert {p.pk for p in side2} == {b.pk, c.pk}


@pytest.mark.django_db
class TestReplayReproducesLiveState:
    """KAN-73 acceptance: live incremental writes == a replay of the history."""

    def test_dry_run_reports_zero_differing_rows_after_live_confirmations(self):
        club = ClubFactory()
        season = SeasonFactory(club=club, starts_on=date(2026, 1, 5), ends_on=date(2026, 1, 20))
        a, b, c, d = (PlayerFactory(club=club) for _ in range(4))
        start = _at(2026, 1, 1)
        for day in range(30):  # confirmed live, in play order
            when = start + timedelta(days=day)
            if day % 4 == 3:
                _finish([a, b], [c, d], club=club, when=when,
                        winner=Side.TWO if day % 8 == 7 else Side.ONE)
            else:
                _finish([a], [[b, c, d][day % 3]], club=club, when=when,
                        winner=Side.ONE if day % 3 else Side.TWO)
        scopes = set(PlayerRating.all_objects.values_list("season_id", "format"))
        assert scopes == {(None, "singles"), (None, "doubles"),
                          (season.pk, "singles"), (season.pk, "doubles")}
        out = StringIO()

        call_command("recalculate_ratings", "--dry-run", stdout=out)

        text = out.getvalue()
        match = re.search(r"Dry run: (\d+) scope rows differ \((\d+) unexplained, (\d+) explained", text)
        assert match, text
        assert match.groups() == ("0", "0", "0")
        assert "Per-player differences" not in text

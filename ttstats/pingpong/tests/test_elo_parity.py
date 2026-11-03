"""The Elo parity harness (KAN-45 / Q4.8).

The harness replays the confirmed history through a frozen copy of the legacy
arithmetic and through the live pipeline, and must agree with itself to the
integer. These tests prove three things: it passes on awkward history, it
*fails* when the arithmetic is deliberately altered, and the figure a user is
shown before confirming is the figure written and recorded.
"""
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from ..elo import projected_elo_changes
from ..elo_parity import run_parity
from ..elo_replay import iter_replay_matches
from ..models import EloHistory, Match, Side
from .conftest import ChampionshipFactory, GameFactory, MatchFactory, PlayerFactory

BASE = timezone.make_aware(datetime(2025, 1, 1, 12, 0, 0))


def _play(side1, side2, *, day=0, winner=Side.ONE, best_of=5, match_type="casual", **kwargs):
    """A finished match. Players without accounts auto-confirm, so live Elo
    is applied the moment the winning game is saved."""
    match = MatchFactory(
        team1_players=list(side1),
        team2_players=list(side2),
        is_double=len(side1) > 1,
        best_of=best_of,
        match_type=match_type,
        date_played=BASE + timedelta(days=day),
        **kwargs,
    )
    needed = best_of // 2 + 1
    for n in range(1, needed + 1):
        if winner == Side.ONE:
            GameFactory(match=match, game_number=n, team1_score=11, team2_score=6)
        else:
            GameFactory(match=match, game_number=n, team1_score=6, team2_score=11)
    match.refresh_from_db()
    return match


def _awkward_history():
    """Every edge the ticket lists: doubles, tournament, best-of 3 and 7, a
    player crossing the 20-match provisional boundary, matches confirmed out
    of play order, ties on date_played, and a player with no matches."""
    ada, bob, cy, di = (PlayerFactory(name=n) for n in ("Ada", "Bob", "Cy", "Di"))
    PlayerFactory(name="Never Played")

    # Confirmed out of play order: day 30 before day 1.
    _play([ada], [bob], day=30, winner=Side.TWO)
    _play([ada], [bob], day=1)

    # Ada crosses the 20-match boundary.
    for i in range(22):
        _play([ada], [cy], day=2 + i % 5, winner=Side.ONE if i % 3 else Side.TWO,
              best_of=(3, 5, 7)[i % 3])

    _play([ada], [di], day=10, match_type="tournament", best_of=7)
    _play([ada, bob], [cy, di], day=11)
    _play([cy, di], [ada, bob], day=11, winner=Side.TWO, match_type="tournament")
    return ada, bob, cy, di


@pytest.mark.django_db
class TestParityHarnessPasses:
    def test_agrees_with_itself_over_awkward_history(self):
        _awkward_history()

        report = run_parity()

        assert report.ok, report.render()
        assert report.matches_replayed == Match.all_objects.filter(winner_side__isnull=False).count()
        assert report.player_diffs == []
        assert report.match_diffs == []

    def test_a_player_with_no_matches_is_compared_at_the_defaults(self):
        PlayerFactory(name="Never Played")

        report = run_parity()

        assert report.ok
        assert report.players_compared == 1

    def test_reports_a_table_and_a_summary_line(self):
        _awkward_history()

        text = run_parity().render()

        assert "PASS" in text
        assert "matches replayed" in text


@pytest.mark.django_db
class TestParityHarnessDetectsDivergence:
    def test_an_injected_k_factor_fails(self):
        _awkward_history()

        report = run_parity(inject_k_factor=40)

        assert not report.ok
        assert report.player_diffs
        assert {d.field for d in report.match_diffs} >= {"k_factor", "rating_change"}

    def test_the_failure_names_players_in_a_diff_table(self):
        _awkward_history()

        text = run_parity(inject_k_factor=40).render()

        assert "FAIL" in text
        assert "Ada" in text
        assert "reference" in text and "pipeline" in text


@pytest.mark.django_db
class TestReplayTraversal:
    def test_order_is_date_played_then_created_at_then_pk(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        later = _play([p1], [p2], day=5)
        tie_a = _play([p1], [p2], day=1)
        tie_b = _play([p1], [p2], day=1)
        # Force an exact tie on both sort keys so only pk can break it.
        Match.all_objects.filter(pk__in=[tie_a.pk, tie_b.pk]).update(created_at=BASE)

        order = [m.pk for m in iter_replay_matches(chunk_size=1)]

        assert order == [tie_a.pk, tie_b.pk, later.pk]

    def test_only_confirmed_matches_with_a_winner_are_in_scope(self):
        """Championship matches can have a winner while unconfirmed."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        for p in (p1, p2):
            p.user.profile.email_verified = True
            p.user.profile.save()
        champ = ChampionshipFactory(with_entries=[[p1], [p2]])
        unconfirmed = _play([p1], [p2], championship=champ)
        assert unconfirmed.winner_side and not unconfirmed.is_confirmed
        confirmed = _play([PlayerFactory()], [PlayerFactory()])

        assert [m.pk for m in iter_replay_matches()] == [confirmed.pk]

    def test_participants_are_attached_per_side(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        _play([a, b], [c, d])

        (record,) = iter_replay_matches()

        side1, side2 = record.sides()
        assert {p.pk for p in side1} == {a.pk, b.pk}
        assert {p.pk for p in side2} == {c.pk, d.pk}


@pytest.mark.django_db
class TestPreviewWriteRecordIdentity:
    """The number a user is asked to agree to is the number written and the
    K recorded. CLAUDE.md: a divergence here is the app lying about a
    decision it asked the user to make."""

    def test_preview_before_confirmation_is_what_gets_recorded(self):
        from .conftest import confirm_match

        players = [PlayerFactory(with_user=True) for _ in range(2)]
        for p in players:
            p.user.profile.email_verified = True
            p.user.profile.save()
        match = _play([players[0]], [players[1]], match_type="tournament", best_of=7)
        preview = projected_elo_changes(match)

        confirm_match(match)

        rows = {h.player_id: h for h in EloHistory.objects.filter(match=match)}
        assert rows[players[0].pk].rating_change == preview.side1_change
        assert rows[players[1].pk].rating_change == preview.side2_change
        assert rows[players[0].pk].k_factor == preview.side1_k
        assert rows[players[1].pk].k_factor == preview.side2_k

    def test_history_confirmed_in_play_order_matches_the_reference_replay(self):
        """Live writes, replayed: every stored EloHistory row equals the frozen
        reference's per-match delta and K."""
        _awkward_history_in_play_order()

        report = run_parity(compare_stored_history=True)

        assert report.ok, report.render()
        assert report.stored_history_diffs == []


def _awkward_history_in_play_order():
    ada, bob, cy = (PlayerFactory() for _ in range(3))
    for i in range(24):
        _play([ada], [bob if i % 2 else cy], day=i, winner=Side.ONE if i % 3 else Side.TWO,
              best_of=(3, 5, 7)[i % 3], match_type="tournament" if i % 4 == 0 else "casual")
    _play([ada, bob], [cy, PlayerFactory()], day=30)


@pytest.mark.django_db
class TestEloParityCommand:
    def test_passes_and_prints_the_summary(self):
        _awkward_history()
        out = StringIO()

        call_command("elo_parity", stdout=out)

        assert "PASS" in out.getvalue()

    def test_injected_k_factor_fails_the_command(self):
        _awkward_history()
        out = StringIO()

        with pytest.raises(CommandError):
            call_command("elo_parity", "--inject-k-factor", "40", stdout=out)

        assert "FAIL" in out.getvalue()
        assert "Ada" in out.getvalue()


@pytest.mark.django_db
class TestSinglesOnlyInvariant:
    """KAN-47: for singles-only players (in a doubles-free component of the
    match graph) the all-time singles PlayerRating equals the legacy value."""

    def _isolated_singles_pair(self):
        eve, fay = PlayerFactory(name="Eve"), PlayerFactory(name="Fay")
        for i in range(23):  # across the provisional boundary
            _play([eve], [fay], day=i, winner=Side.ONE if i % 3 else Side.TWO,
                  best_of=(3, 5, 7)[i % 3], match_type="tournament" if i % 5 == 0 else "casual")
        return eve, fay

    def test_singles_only_players_are_checked_and_agree(self):
        _awkward_history()  # every player here is in a component with doubles
        self._isolated_singles_pair()

        report = run_parity()

        assert report.ok, report.render()
        assert report.singles_only_checked == 2

    def test_a_singles_only_player_who_met_a_doubles_player_is_not_checked(self):
        a, b, c, d = (PlayerFactory() for _ in range(4))
        _play([a, b], [c, d], day=0)
        solo = PlayerFactory()
        _play([solo], [a], day=1)

        report = run_parity()

        assert report.ok
        assert report.singles_only_checked == 0

    def test_a_divergent_format_scope_fails_the_harness(self):
        from .. import elo

        self._isolated_singles_pair()
        real = elo.scope_reading

        def off_by_one(book, player_id, scope):
            rating, experience = real(book, player_id, scope)
            return rating + 1, experience

        with mock.patch.object(elo, "scope_reading", side_effect=off_by_one):
            report = run_parity()

        assert not report.ok
        assert {d.scope for d in report.player_diffs} == {"singles all-time"}

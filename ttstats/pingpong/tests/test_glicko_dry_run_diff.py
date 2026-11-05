"""`recalculate_ratings --dry-run` diffs the stored Glicko-2 state (KAN-69).

This is the live-versus-replay check that gates the cutover: the stored state
must equal what a replay of the history would write.
"""

import re
from io import StringIO

import pytest
from django.core.management import call_command

from ..models import PlayerRating
from .conftest import PlayerFactory
from .test_glicko_shadow import _at, _finish, _row, _state


def _dry_run(*args):
    out = StringIO()
    call_command("recalculate_ratings", "--dry-run", *args, stdout=out)
    return out.getvalue()


def _summary(text):
    """(rows that differ, unexplained, explained) from the dry-run summary line."""
    m = re.search(
        r"Dry run: (\d+) scope rows? differ \((\d+) unexplained, (\d+) explained",
        text,
    )
    assert m, text
    return tuple(int(g) for g in m.groups())


def _in_order_history():
    p1, p2, p3 = PlayerFactory(), PlayerFactory(), PlayerFactory()
    for when, (a, b) in zip(
        [_at(2026, 1, 3), _at(2026, 1, 3), _at(2026, 1, 20), _at(2026, 4, 2)],
        [(p1, p2), (p2, p3), (p1, p3), (p3, p1)],
    ):
        _finish([a], [b], when=when)
    return p1, p2, p3


def _shadow():
    return {r.pk: _state(r) for r in PlayerRating.all_objects.order_by("pk")}


@pytest.mark.django_db
class TestGlickoShadowDiff:
    def test_zero_diffs_after_live_confirmations_in_date_order(self):
        _in_order_history()

        text = _dry_run()

        assert _summary(text) == (0, 0, 0)

    @pytest.mark.parametrize("field", ["glicko_rating", "rd", "volatility"])
    def test_a_tampered_float_is_reported_per_player_and_scope(self, field):
        p1, _p2, _p3 = _in_order_history()
        row = _row(p1)
        PlayerRating.all_objects.filter(pk=row.pk).update(
            **{field: getattr(row, field) + 0.01}
        )

        text = _dry_run()

        assert _summary(text) == (1, 1, 0)
        line = next(
            ln
            for ln in text.splitlines()
            if ln.strip().startswith(p1.name) and f"{field}:" in ln
        )
        assert "singles" in line

    def test_a_tampered_last_rated_on_is_reported(self):
        p1, _p2, _p3 = _in_order_history()
        PlayerRating.all_objects.filter(
            player=p1, season=None, format="singles"
        ).update(last_rated_on=_at(2020, 1, 1).date())

        assert _summary(_dry_run())[0] == 1

    def test_a_difference_inside_the_tolerance_is_not_reported(self):
        p1, _p2, _p3 = _in_order_history()
        row = _row(p1)
        PlayerRating.all_objects.filter(pk=row.pk).update(rd=row.rd + 1e-9)

        assert _summary(_dry_run()) == (0, 0, 0)

    def test_a_null_shadow_is_reported_against_the_replayed_value(self):
        p1, _p2, _p3 = _in_order_history()
        PlayerRating.all_objects.filter(player=p1, format="singles").update(
            glicko_rating=None, rd=None, volatility=None, last_rated_on=None
        )

        text = _dry_run()

        assert _summary(text)[0] == 1
        assert "None ->" in text

    def test_dry_run_writes_nothing(self):
        p1, _p2, _p3 = _in_order_history()
        PlayerRating.all_objects.filter(player=p1, format="singles").update(rd=999.0)
        before = _shadow()

        _dry_run()

        assert _shadow() == before

    def test_the_format_scope_limits_the_glicko_diff(self):
        p1, _p2, _p3 = _in_order_history()
        PlayerRating.all_objects.filter(player=p1, format="singles").update(rd=999.0)

        assert _summary(_dry_run("--format", "doubles"))[0] == 0


@pytest.mark.django_db
class TestOutOfOrderConfirmationsAreListedSeparately:
    def test_a_match_confirmed_out_of_date_order_explains_its_diff(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        _finish([p1], [p2], when=_at(2026, 3, 10))
        late = _finish([p1], [p2], when=_at(2026, 3, 1))  # applied second, played first

        text = _dry_run()

        rows, unexplained, explained = _summary(text)
        assert rows > 0
        assert unexplained == 0
        assert explained == rows
        assert "Matches confirmed out of date order (1)" in text
        assert f"match {late.pk}" in text

    def test_a_genuine_divergence_stays_unexplained(self):
        _p1, _p2, p3 = _in_order_history()
        PlayerRating.all_objects.filter(player=p3, format="singles").update(rd=999.0)

        text = _dry_run()

        assert _summary(text) == (1, 1, 0)
        assert "Matches confirmed out of date order" not in text

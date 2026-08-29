"""Query ceilings for the match-completion write path.

Saving the final Game of a match fans out through three `post_save` receivers,
the Elo pipeline and every achievement checker. That is the app's most
expensive single write, and it cost 114 queries for a two-player
match -- growing by two more for every match already in the players' history,
because `check_rivalry` resolved each opponent with its own query.

The growth test is the important one. A ceiling that holds at 60 matches of
history but not at 600 is not a ceiling; the assertion here is that the cost
per save is *flat*, so a regression shows up as a slope rather than as a
number somebody has to remember.

Same shape as `test_match_list_performance.py`, which pins the list view.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from .conftest import GameFactory, MatchFactory, PlayerFactory, confirm_match_silent


def _finish_match(player1, player2, best_of=3):
    """Play a match to its penultimate game and return it, unfinished."""
    match = MatchFactory(player1=player1, player2=player2, best_of=best_of)
    GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
    return match


def _build_history(player1, player2, count):
    """`count` completed, confirmed matches between the two players."""
    for _ in range(count):
        match = MatchFactory(player1=player1, player2=player2, best_of=3)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        confirm_match_silent(match)


@pytest.mark.django_db
class TestMatchCompletionQueryCount:
    """The write path that fires when a match reaches its winning game."""

    # Measured at 39 queries after the rework, from 114. The headroom
    # is deliberate: it must not be so tight that adding one legitimate field
    # fails CI, nor so loose that the N+1 could come back unnoticed.
    CEILING = 40

    def test_saving_the_winning_game_stays_under_the_ceiling(self):
        # ARRANGE
        p1 = PlayerFactory(name="Ada", with_user=True)
        p2 = PlayerFactory(name="Bob", with_user=True)
        match = _finish_match(p1, p2)

        # ACT: this game takes the match to 2-0 in a best of 3, so it triggers
        # winner detection, confirmation, Elo and every achievement checker.
        with CaptureQueriesContext(connection) as ctx:
            GameFactory(match=match, game_number=2, team1_score=11, team2_score=5)

        # ASSERT
        assert len(ctx.captured_queries) <= self.CEILING, (
            f"Saving the winning game took {len(ctx.captured_queries)} queries, "
            f"expected <= {self.CEILING}. The match-completion signal pipeline "
            f"has regressed -- see achievements.PlayerContext, Match.sides() "
            f"and Match._confirmation_state."
        )

    def test_cost_does_not_grow_with_match_history(self):
        """The linear term is the regression that actually hurts.

        `check_rivalry` and `check_win_streak` walk the player's whole career.
        While they each re-queried, every match ever played added queries to
        every future save. This asserts the slope is flat, not merely that one
        sample is small.
        """
        # ARRANGE
        p1 = PlayerFactory(name="Ada", with_user=True)
        p2 = PlayerFactory(name="Bob", with_user=True)

        _build_history(p1, p2, count=5)
        short = _finish_match(p1, p2)
        with CaptureQueriesContext(connection) as ctx_short:
            GameFactory(match=short, game_number=2, team1_score=11, team2_score=5)

        _build_history(p1, p2, count=35)  # 40 matches of history now
        long = _finish_match(p1, p2)
        with CaptureQueriesContext(connection) as ctx_long:
            GameFactory(match=long, game_number=2, team1_score=11, team2_score=5)

        # ASSERT: 35 extra matches must not cost extra queries.
        short_count = len(ctx_short.captured_queries)
        long_count = len(ctx_long.captured_queries)
        assert long_count <= short_count, (
            f"Query count grew with history: {short_count} queries at 5 prior "
            f"matches, {long_count} at 40. Something in the achievement or Elo "
            f"path is querying per historical match."
        )

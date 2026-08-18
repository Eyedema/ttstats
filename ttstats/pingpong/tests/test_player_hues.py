"""Player identity hues.

A hue is not decoration in this design: it is the primary "who" signal on the
dashboard, the leaderboard and the scoreboard, so the guarantees that matter
are that it is *stable* and that it is *never absent*.

The pure-function tests need no database, matching how match_state.py and
live_scoring.py are covered.
"""
import pytest

from pingpong import player_hues
from pingpong.templatetags.player_tags import hue

from .conftest import PlayerFactory


class TestHueAssignment:
    """Pure: no Django, no DB."""

    def test_every_slot_declares_a_hue_and_an_ink(self):
        for name, colour, ink in player_hues.HUES:
            assert name
            assert colour.startswith("#") and len(colour) == 7
            assert ink.startswith("#") and len(ink) == 7

    def test_index_is_one_based_and_wraps(self):
        assert player_hues.hue_index(1) == 1
        assert player_hues.hue_index(player_hues.HUE_COUNT) == player_hues.HUE_COUNT
        # The ninth player starts the palette again rather than falling off it.
        assert player_hues.hue_index(player_hues.HUE_COUNT + 1) == 1

    def test_consecutive_players_never_share_a_hue(self):
        """Two people who play each other must be told apart by colour alone.

        Sequential primary keys are how a real club fills up, so the mapping
        has to spread over that ordering rather than merely being uniform.
        """
        window = [player_hues.hue_index(pk) for pk in range(1, player_hues.HUE_COUNT + 1)]
        assert len(set(window)) == player_hues.HUE_COUNT

    def test_a_missing_player_still_gets_a_hue(self):
        """A deleted participant leaves None in the template.

        MatchParticipant.player cascades on delete, so a match can render with
        an empty side. Returning slot 1 rather than raising means the bar draws
        in a defined colour instead of inheriting whatever `--hue` an ancestor
        happened to set.
        """
        assert player_hues.hue_index(None) == 1
        assert player_hues.hue_class(None) == "hue-1"
        assert player_hues.hue_hex(None).startswith("#")
        assert player_hues.hue_ink(None).startswith("#")

    def test_class_matches_the_index(self):
        for pk in range(1, 20):
            assert player_hues.hue_class(pk) == f"hue-{player_hues.hue_index(pk)}"


@pytest.mark.django_db
class TestPlayerHueProperties:
    def test_player_exposes_its_hue(self):
        player = PlayerFactory()
        assert player.hue_class == player_hues.hue_class(player.pk)
        assert player.hue_hex == player_hues.hue_hex(player.pk)
        assert player.hue_ink == player_hues.hue_ink(player.pk)

    def test_hue_survives_a_rename(self):
        """The hue is keyed on the pk, so it is an identity, not a label."""
        player = PlayerFactory(name="Before")
        before = player.hue_class
        player.name = "After"
        player.save()
        player.refresh_from_db()
        assert player.hue_class == before

    def test_filter_handles_a_player_and_a_none(self):
        player = PlayerFactory()
        assert hue(player) == player.hue_class
        assert hue(None) == "hue-1"

"""
Elo rating calculation for TTStats.
Uses traditional Elo formula with table tennis-specific K-factor adjustments.
Supports both 1v1 and 2v2 matches.
"""

import logging
from typing import NamedTuple

from django.db import transaction

logger = logging.getLogger(__name__)

def calculate_k_factor(match, player):
    """
    Calculate K-factor based on match importance and player experience.
    Returns higher K for:
    - Tournament matches (more important)
    - Longer matches (more reliable result)
    - New players (first 20 matches)
    """
    base_k = 32  # Standard chess K-factor

    # Match type multiplier
    if match.match_type == 'tournament':
        type_multiplier = 1.5  # Tournament matches matter more
    else:
        type_multiplier = 1.0  # Practice and casual are equal

    # Best-of multiplier (longer matches are more conclusive)
    best_of_multipliers = {
        3: 0.9,
        5: 1.0,
        7: 1.1,
    }
    best_of_multiplier = best_of_multipliers.get(match.best_of, 1.0)

    # New player boost (higher K for first 20 matches)
    if player.matches_for_elo < 20:
        experience_multiplier = 1.5
    else:
        experience_multiplier = 1.0

    k = base_k * type_multiplier * best_of_multiplier * experience_multiplier
    return k

def calculate_expected_score(rating_a, rating_b):
    """Calculate expected score (probability of A winning) using Elo formula."""
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))

def calculate_elo_change(r1, r2, actual_score, k_factor):
    """Helper to calculate raw Elo point change."""
    expected = calculate_expected_score(r1, r2)
    return round(k_factor * (actual_score - expected))


def get_win_probability(side1_players, side2_players, match=None):
    """Return (side1_pct, side2_pct) as integer percentages.

    Takes the players on each side directly, so it works for a Match, a
    ScheduledMatch, or a hypothetical pairing that isn't stored at all.

    If match is provided and has EloHistory records, uses pre-match ratings
    so the prediction reflects what was expected before the match was played.
    Otherwise uses current player ratings.
    """
    from .models import EloHistory

    team1_players = list(side1_players)
    team2_players = list(side2_players)

    if not team1_players or not team2_players:
        return (50, 50)

    # Try to use pre-match ratings from EloHistory
    if match:
        elo_map = {
            eh.player_id: eh.old_rating
            for eh in EloHistory.objects.filter(match=match)
        }
        if elo_map:
            r1 = sum(elo_map.get(p.pk, p.elo_rating) for p in team1_players) / len(team1_players)
            r2 = sum(elo_map.get(p.pk, p.elo_rating) for p in team2_players) / len(team2_players)
        else:
            r1 = sum(p.elo_rating for p in team1_players) / len(team1_players)
            r2 = sum(p.elo_rating for p in team2_players) / len(team2_players)
    else:
        r1 = sum(p.elo_rating for p in team1_players) / len(team1_players)
        r2 = sum(p.elo_rating for p in team2_players) / len(team2_players)

    prob = calculate_expected_score(r1, r2)
    t1_pct = round(prob * 100)
    t2_pct = 100 - t1_pct
    return (t1_pct, t2_pct)


class EloProjection(NamedTuple):
    """What a match is worth, before anyone has agreed to it.

    Carries the K-factors as well as the changes because EloHistory records the
    K it was calculated with, and the only way to guarantee the recorded K is
    the K actually used is to hand back both from one call.
    """

    side1_change: int
    side2_change: int
    side1_k: float
    side2_k: float


def projected_elo_changes(match):
    """An EloProjection for a match, calculated but not applied.

    The dashboard tells you what a pending confirmation costs -- "-16 Elo if
    true" -- before you agree to it, and the match detail shows the same figure
    next to each name. That number has to be the number that will actually be
    written when both players confirm, so this is the arithmetic
    `update_player_elo` uses rather than a second copy of it.

    Zero changes when the match has no winner or a side is empty; a match
    nobody has won yet moves nobody's rating.
    """
    from .models import Side

    nothing = EloProjection(0, 0, 0.0, 0.0)

    if not match.winner_side:
        return nothing

    team1_players = list(match.players_on(Side.ONE))
    team2_players = list(match.players_on(Side.TWO))
    if not team1_players or not team2_players:
        return nothing

    r1 = sum(p.elo_rating for p in team1_players) / len(team1_players)
    r2 = sum(p.elo_rating for p in team2_players) / len(team2_players)

    s1 = 1 if match.winner_side == Side.ONE else 0
    s2 = 1 - s1

    # For 2v2 the team K-factor is the average of its members', which
    # preserves the new-player boost inside a team.
    k1_list = [calculate_k_factor(match, p) for p in team1_players]
    k2_list = [calculate_k_factor(match, p) for p in team2_players]
    k1_team = sum(k1_list) / len(k1_list)
    k2_team = sum(k2_list) / len(k2_list)

    return EloProjection(
        calculate_elo_change(r1, r2, s1, k1_team),
        calculate_elo_change(r2, r1, s2, k2_team),
        k1_team,
        k2_team,
    )


def projected_elo_change_for(match, player):
    """What this match is worth to one player, signed. 0 if they did not play."""
    from .models import Side

    if player is None:
        return 0
    projection = projected_elo_changes(match)
    if any(p.pk == player.pk for p in match.players_on(Side.ONE)):
        return projection.side1_change
    if any(p.pk == player.pk for p in match.players_on(Side.TWO)):
        return projection.side2_change
    return 0


def update_player_elo(match):
    """
    Calculate and update Elo ratings after match completion.
    Called from handle_match_completion signal or management command.
    
    Handles both 1v1 and 2v2 matches.
    For 2v2, uses the average Elo of the team to calculate probabilities,
    then applies the same rating change to both players on the team.
    """
    from .models import EloHistory, Match, Side

    # Guard: Must have winner
    if not match.winner_side:
        logger.debug(f"Skipping Elo update for match {match.pk}: no winner")
        return

    # Guard: Must be confirmed
    if not match.match_confirmed:
        logger.debug(
            f"Skipping Elo update for match {match.pk}: not confirmed "
            f"(side1={match.team1_confirmed}, side2={match.team2_confirmed})"
        )
        return

    # Guard: Skip if Elo already calculated for this match
    if EloHistory.objects.filter(match=match).exists():
        logger.debug(f"Skipping Elo update for match {match.pk}: already calculated")
        return

    with transaction.atomic():
        # 1. IDENTIFY SIDES AND PLAYERS
        team1_players = list(match.players_on(Side.ONE))
        team2_players = list(match.players_on(Side.TWO))

        is_double = match.is_double

        # An empty side means there is nothing to rate, singles or doubles.
        if not team1_players or not team2_players:
            logger.error(f"Skipping Elo update for match {match.pk}: empty side found")
            return

        # 2-4. THE ARITHMETIC
        # Shared with projected_elo_changes(), which is what the dashboard and
        # match detail show as "-16 Elo if true". The figure the user is asked
        # to agree to must be the figure that gets written.
        elo_change_1, elo_change_2, k1_team, k2_team = projected_elo_changes(match)

        # 5. APPLY UPDATES TO TEAM 1
        for p in team1_players:
            old_rating = p.elo_rating
            p.elo_rating += elo_change_1
            p.elo_peak = max(p.elo_peak, p.elo_rating)
            p.matches_for_elo += 1
            p.save(update_fields=['elo_rating', 'elo_peak', 'matches_for_elo'])
            
            # Record History
            EloHistory.objects.create(
                match=match,
                player=p,
                old_rating=old_rating,
                new_rating=p.elo_rating,
                rating_change=elo_change_1,
                k_factor=k1_team 
            )

        # 6. APPLY UPDATES TO TEAM 2
        for p in team2_players:
            old_rating = p.elo_rating
            p.elo_rating += elo_change_2
            p.elo_peak = max(p.elo_peak, p.elo_rating)
            p.matches_for_elo += 1
            p.save(update_fields=['elo_rating', 'elo_peak', 'matches_for_elo'])
            
            # Record History
            EloHistory.objects.create(
                match=match,
                player=p,
                old_rating=old_rating,
                new_rating=p.elo_rating,
                rating_change=elo_change_2,
                k_factor=k2_team
            )

    # Logging
    team1_names = ", ".join([p.name for p in team1_players])
    team2_names = ", ".join([p.name for p in team2_players])
    
    logger.info(
        f"Elo updated for match {match.pk} ({'2v2' if is_double else '1v1'}): "
        f"Team1[{team1_names}] {elo_change_1:+d}, "
        f"Team2[{team2_names}] {elo_change_2:+d}"
    )

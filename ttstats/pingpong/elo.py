"""
Elo rating calculation for TTStats.
Uses traditional Elo formula with table tennis-specific K-factor adjustments.
"""

import logging

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


def update_player_elo(match):
    """
    Calculate and update Elo ratings after match completion.
    Called from handle_match_completion signal.

    Guards:
    - Only runs if match has winner
    - Only runs if match is confirmed by both players
    - Skips 2v2 matches (future: friend will implement)

    Future compatibility:
    - Checks for 'is_double' field (doesn't exist in master, but will in support_doubles)
    - For 1v1: Uses match.player1 and match.player2 (current structure)
    - For 2v2: Will need to extract players from match.team1/team2 (friend's work)
    """
    from .models import EloHistory, Match

    # Guard: Must have winner
    if not match.winner:
        logger.debug(f"Skipping Elo update for match {match.pk}: no winner")
        return

    # Guard: Must be confirmed
    if not match.match_confirmed:
        logger.debug(
            f"Skipping Elo update for match {match.pk}: not confirmed "
            f"(player1={match.player1_confirmed}, player2={match.player2_confirmed})"
        )
        return

    # Guard: Skip if Elo already calculated for this match
    if EloHistory.objects.filter(match=match).exists():
        logger.debug(f"Skipping Elo update for match {match.pk}: already calculated")
        return

    # Future-proofing: Skip 2v2 matches (friend will implement calculate_elo_2v2)
    if hasattr(match, 'is_double') and match.is_double:
        logger.info(f"Skipping Elo update for match {match.pk}: 2v2 not yet implemented")
        return

    # Get players (current master structure uses player1/player2)
    # Note: In support_doubles branch, this will need refactoring to:
    #   player1 = match.team1.players.first()
    #   player2 = match.team2.players.first()
    player1 = match.team1.players.first()
    player2 = match.team2.players.first()

    # Get current ratings
    r1 = player1.elo_rating
    r2 = player2.elo_rating

    # Calculate expected scores (probability of winning)
    e1 = calculate_expected_score(r1, r2)
    e2 = 1 - e1  # Probabilities sum to 1

    # Actual scores (1 = win, 0 = loss)
    s1 = 1 if match.winner.players.first() == player1 else 0
    s2 = 1 - s1

    # Calculate K-factors (can be different for each player due to experience)
    k1 = calculate_k_factor(match, player1)
    k2 = calculate_k_factor(match, player2)

    # Calculate rating changes
    elo_change_1 = round(k1 * (s1 - e1))
    elo_change_2 = round(k2 * (s2 - e2))

    # Store old ratings
    old_rating_1 = player1.elo_rating
    old_rating_2 = player2.elo_rating

    # Update player ratings
    player1.elo_rating += elo_change_1
    player1.elo_peak = max(player1.elo_peak, player1.elo_rating)
    player1.matches_for_elo += 1
    player1.save(update_fields=['elo_rating', 'elo_peak', 'matches_for_elo'])

    player2.elo_rating += elo_change_2
    player2.elo_peak = max(player2.elo_peak, player2.elo_rating)
    player2.matches_for_elo += 1
    player2.save(update_fields=['elo_rating', 'elo_peak', 'matches_for_elo'])

    # Create history records
    EloHistory.objects.create(
        match=match,
        player=player1,
        old_rating=old_rating_1,
        new_rating=player1.elo_rating,
        rating_change=elo_change_1,
        k_factor=k1,
    )

    EloHistory.objects.create(
        match=match,
        player=player2,
        old_rating=old_rating_2,
        new_rating=player2.elo_rating,
        rating_change=elo_change_2,
        k_factor=k2,
    )

    logger.info(
        f"Elo updated for match {match.pk}: "
        f"{player1} {elo_change_1:+d} ({old_rating_1} → {player1.elo_rating}), "
        f"{player2} {elo_change_2:+d} ({old_rating_2} → {player2.elo_rating})"
    )

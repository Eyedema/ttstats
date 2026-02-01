"""
Elo rating calculation for TTStats.
Uses traditional Elo formula with table tennis-specific K-factor adjustments.
Supports both 1v1 and 2v2 matches.
"""

import logging
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

def update_player_elo(match):
    """
    Calculate and update Elo ratings after match completion.
    Called from handle_match_completion signal or management command.
    
    Handles both 1v1 and 2v2 matches.
    For 2v2, uses the average Elo of the team to calculate probabilities,
    then applies the same rating change to both players on the team.
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
            f"(team1={match.team1_confirmed}, team2={match.team2_confirmed})"
        )
        return

    # Guard: Skip if Elo already calculated for this match
    if EloHistory.objects.filter(match=match).exists():
        logger.debug(f"Skipping Elo update for match {match.pk}: already calculated")
        return

    with transaction.atomic():
        # 1. IDENTIFY TEAMS AND PLAYERS
        # We determine if it's 2v2 by checking the number of players
        team1_players = list(match.team1.players.all())
        team2_players = list(match.team2.players.all())
        
        is_double = len(team1_players) > 1 or len(team2_players) > 1

        if is_double:
            # Calculate Team Elo (Average)
            # If a team has 0 players (shouldn't happen), avoid division by zero
            if not team1_players or not team2_players:
                logger.error(f"Skipping Elo update for match {match.pk}: empty team found")
                return

            r1 = sum(p.elo_rating for p in team1_players) / len(team1_players)
            r2 = sum(p.elo_rating for p in team2_players) / len(team2_players)
        else:
            # 1v1 Case
            r1 = team1_players[0].elo_rating
            r2 = team2_players[0].elo_rating

        # 2. DETERMINE OUTCOME (1 = Team 1 wins, 0 = Team 2 wins)
        # We check if the winner object is the same as the team1 object
        if match.winner == match.team1:
            s1 = 1
        else:
            s1 = 0
        s2 = 1 - s1

        # 3. CALCULATE K-FACTORS
        # For 2v2, we average the K-factors of the players in the team
        # This preserves the "new player boost" logic even in teams
        k1_list = [calculate_k_factor(match, p) for p in team1_players]
        k2_list = [calculate_k_factor(match, p) for p in team2_players]
        
        k1_team = sum(k1_list) / len(k1_list)
        k2_team = sum(k2_list) / len(k2_list)

        # 4. CALCULATE CHANGE
        elo_change_1 = calculate_elo_change(r1, r2, s1, k1_team)
        elo_change_2 = calculate_elo_change(r2, r1, s2, k2_team)

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

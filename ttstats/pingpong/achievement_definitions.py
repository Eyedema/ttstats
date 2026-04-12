"""
Achievement definitions for TTStats.
Used by data migration and management command to seed the Achievement table.
"""

ACHIEVEMENT_DEFINITIONS = [
    # --- Tiered: Win Streak ---
    {
        'slug': 'win_streak_bronze', 'name': 'Win Streak', 'group': 'win_streak',
        'description': 'Win 3 matches in a row',
        'tier': 'bronze', 'icon': 'flame', 'threshold': 3, 'sort_order': 1,
    },
    {
        'slug': 'win_streak_silver', 'name': 'Win Streak', 'group': 'win_streak',
        'description': 'Win 5 matches in a row',
        'tier': 'silver', 'icon': 'flame', 'threshold': 5, 'sort_order': 2,
    },
    {
        'slug': 'win_streak_gold', 'name': 'Win Streak', 'group': 'win_streak',
        'description': 'Win 10 matches in a row',
        'tier': 'gold', 'icon': 'flame', 'threshold': 10, 'sort_order': 3,
    },
    # --- Tiered: Matches Played ---
    {
        'slug': 'matches_played_bronze', 'name': 'Matches Played', 'group': 'matches_played',
        'description': 'Play 10 matches',
        'tier': 'bronze', 'icon': 'swords', 'threshold': 10, 'sort_order': 1,
    },
    {
        'slug': 'matches_played_silver', 'name': 'Matches Played', 'group': 'matches_played',
        'description': 'Play 50 matches',
        'tier': 'silver', 'icon': 'swords', 'threshold': 50, 'sort_order': 2,
    },
    {
        'slug': 'matches_played_gold', 'name': 'Matches Played', 'group': 'matches_played',
        'description': 'Play 100 matches',
        'tier': 'gold', 'icon': 'swords', 'threshold': 100, 'sort_order': 3,
    },
    # --- Tiered: Matches Won ---
    {
        'slug': 'matches_won_bronze', 'name': 'Matches Won', 'group': 'matches_won',
        'description': 'Win 10 matches',
        'tier': 'bronze', 'icon': 'trophy', 'threshold': 10, 'sort_order': 1,
    },
    {
        'slug': 'matches_won_silver', 'name': 'Matches Won', 'group': 'matches_won',
        'description': 'Win 50 matches',
        'tier': 'silver', 'icon': 'trophy', 'threshold': 50, 'sort_order': 2,
    },
    {
        'slug': 'matches_won_gold', 'name': 'Matches Won', 'group': 'matches_won',
        'description': 'Win 100 matches',
        'tier': 'gold', 'icon': 'trophy', 'threshold': 100, 'sort_order': 3,
    },
    # --- Tiered: Deuce Master ---
    {
        'slug': 'deuce_master_bronze', 'name': 'Deuce Master', 'group': 'deuce_master',
        'description': 'Win 3 games that went to deuce',
        'tier': 'bronze', 'icon': 'target', 'threshold': 3, 'sort_order': 1,
    },
    {
        'slug': 'deuce_master_silver', 'name': 'Deuce Master', 'group': 'deuce_master',
        'description': 'Win 10 games that went to deuce',
        'tier': 'silver', 'icon': 'target', 'threshold': 10, 'sort_order': 2,
    },
    {
        'slug': 'deuce_master_gold', 'name': 'Deuce Master', 'group': 'deuce_master',
        'description': 'Win 25 games that went to deuce',
        'tier': 'gold', 'icon': 'target', 'threshold': 25, 'sort_order': 3,
    },
    # --- Tiered: Giant Slayer ---
    {
        'slug': 'giant_slayer_bronze', 'name': 'Giant Slayer', 'group': 'giant_slayer',
        'description': 'Beat someone rated 100+ Elo above you',
        'tier': 'bronze', 'icon': 'sword', 'threshold': 100, 'sort_order': 1,
    },
    {
        'slug': 'giant_slayer_silver', 'name': 'Giant Slayer', 'group': 'giant_slayer',
        'description': 'Beat someone rated 200+ Elo above you',
        'tier': 'silver', 'icon': 'sword', 'threshold': 200, 'sort_order': 2,
    },
    {
        'slug': 'giant_slayer_gold', 'name': 'Giant Slayer', 'group': 'giant_slayer',
        'description': 'Beat someone rated 300+ Elo above you',
        'tier': 'gold', 'icon': 'sword', 'threshold': 300, 'sort_order': 3,
    },
    # --- Tiered: Rivalry ---
    {
        'slug': 'rivalry_bronze', 'name': 'Rivalry', 'group': 'rivalry',
        'description': 'Play the same opponent 5 times',
        'tier': 'bronze', 'icon': 'users', 'threshold': 5, 'sort_order': 1,
    },
    {
        'slug': 'rivalry_silver', 'name': 'Rivalry', 'group': 'rivalry',
        'description': 'Play the same opponent 10 times',
        'tier': 'silver', 'icon': 'users', 'threshold': 10, 'sort_order': 2,
    },
    {
        'slug': 'rivalry_gold', 'name': 'Rivalry', 'group': 'rivalry',
        'description': 'Play the same opponent 20 times',
        'tier': 'gold', 'icon': 'users', 'threshold': 20, 'sort_order': 3,
    },
    # --- One-off achievements ---
    {
        'slug': 'first_blood', 'name': 'First Blood', 'group': 'first_blood',
        'description': 'Win your first match',
        'tier': 'none', 'icon': 'zap', 'threshold': 1, 'sort_order': 0,
    },
    {
        'slug': 'perfect_game', 'name': 'Perfect Game', 'group': 'perfect_game',
        'description': 'Win a game 11-0',
        'tier': 'none', 'icon': 'star', 'threshold': 1, 'sort_order': 0,
    },
    {
        'slug': 'comeback_king', 'name': 'Comeback King', 'group': 'comeback_king',
        'description': 'Win after being down 0-2 in games',
        'tier': 'none', 'icon': 'crown', 'threshold': 1, 'sort_order': 0,
    },
    {
        'slug': 'iron_wall', 'name': 'Iron Wall', 'group': 'iron_wall',
        'description': 'Win a match conceding fewer than 20 total points',
        'tier': 'none', 'icon': 'shield', 'threshold': 1, 'sort_order': 0,
    },
    {
        'slug': 'marathon_match', 'name': 'Marathon Match', 'group': 'marathon_match',
        'description': 'Win a match that goes to the maximum number of games',
        'tier': 'none', 'icon': 'timer', 'threshold': 1, 'sort_order': 0,
    },
    {
        'slug': 'peak_performer', 'name': 'Peak Performer', 'group': 'peak_performer',
        'description': 'Reach a new personal Elo peak',
        'tier': 'none', 'icon': 'trending-up', 'threshold': 1, 'sort_order': 0,
    },
]

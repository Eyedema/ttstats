"""
Achievement detection engine for TTStats.

Each checker function is registered via @register_checker. It receives
(player, match) and returns a list of achievement slugs to award.

Use Match.all_objects throughout to bypass row-level security.
"""

import logging
from collections import Counter

from django.db.models import F, Prefetch, prefetch_related_objects

from .models import Achievement, EloHistory, Game, Match, PlayerAchievement, Side

logger = logging.getLogger(__name__)


class PlayerContext:
    """Everything the checkers need about one player, fetched once.

    Eleven checkers run for every player of every confirmed match. Each used to
    resolve its own inputs, so the player's whole match history was loaded five
    separate times and `check_rivalry` resolved each historical opponent with
    its own query -- making the cost of finishing a match grow forever with how
    much table tennis the players had already played. Measured at 114 queries
    for a two-player match, plus two more for every match in their history.

    Everything below is loaded once and answered from memory afterwards. The
    checkers still accept `ctx=None` and build their own when called directly,
    which is how the existing per-checker tests keep working unchanged.
    """

    def __init__(self, player, match=None):
        self.player = player
        self.match = match
        self._confirmed_matches = None
        self._elo_by_player = None

        if match is not None:
            _ensure_match_prefetched(match)

    @property
    def confirmed_matches(self):
        """The player's confirmed matches, newest first, fully prefetched."""
        if self._confirmed_matches is None:
            self._confirmed_matches = list(_player_confirmed_matches(self.player))
        return self._confirmed_matches

    @property
    def won_count(self):
        """Confirmed matches this player won -- counted in memory."""
        return sum(
            1 for m in self.confirmed_matches if _player_won_match(self.player, m)
        )

    @property
    def elo_by_player(self):
        """This match's EloHistory rows, keyed by player id.

        Two checkers need them (giant slayer, peak performer) and each used to
        fetch its own.
        """
        if self._elo_by_player is None:
            if self.match is None:
                self._elo_by_player = {}
            else:
                self._elo_by_player = {
                    eh.player_id: eh
                    for eh in EloHistory.objects.filter(match=self.match)
                }
        return self._elo_by_player


def _ensure_match_prefetched(match):
    """Load the match's participants and games onto the instance, once.

    `match.participants.all()` issues a query every time it is called -- a
    related manager is not cached the way a prefetch is -- and the checkers ask
    repeatedly, via `_player_side`. Prefetching onto the instance turns all of
    those into dictionary lookups.

    Idempotent: the same match object is passed once per player, and this must
    not re-query for the second player.
    """
    cache = getattr(match, "_prefetched_objects_cache", None)
    if cache is not None and "participants" in cache and "games" in cache:
        return
    prefetch_related_objects(
        [match],
        "participants__player",
        Prefetch("games", queryset=Game.all_objects.all()),
    )

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_CHECKERS = []


def register_checker(fn):
    _CHECKERS.append(fn)
    return fn


def check_achievements_for_player(player, match):
    """Run all checkers for a player after a match is confirmed.
    Returns list of newly awarded PlayerAchievement instances."""
    already_earned = set(
        PlayerAchievement.objects.filter(player=player)
        .values_list('achievement__slug', flat=True)
    )

    achievement_cache = {
        a.slug: a for a in Achievement.objects.all()
    }

    # Built once and handed to every checker, instead of each resolving its
    # own inputs. This is what makes the cost flat in the player's history.
    ctx = PlayerContext(player, match)

    new_awards = []
    for checker in _CHECKERS:
        slugs = checker(player, match, ctx)
        for slug in slugs:
            if slug in already_earned:
                continue
            achievement = achievement_cache.get(slug)
            if not achievement:
                logger.warning('Achievement slug %s not found in DB', slug)
                continue
            pa = PlayerAchievement.objects.create(
                player=player, achievement=achievement, match=match,
            )
            new_awards.append(pa)
            already_earned.add(slug)

    return new_awards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _player_confirmed_matches(player):
    """All confirmed matches for a player, newest first.
    Uses all_objects to bypass row-level security and prefetches
    games via unfiltered manager to bypass GameManager."""
    return (
        Match.all_objects
        .filter(participants__player=player, is_confirmed=True)
        .prefetch_related(
            'participants__player',
            Prefetch('games', queryset=Game.all_objects.all()),
        )
        .order_by('-date_played')
    )


def _player_won_count(player, ctx=None):
    """How many confirmed matches the player has won.

    Both conditions live in one filter() so they apply to the same
    participant row -- the player's own side must be the winning one.

    With a context, the answer comes from the already-loaded match list instead
    of a second round trip -- two checkers ask for it.
    """
    if ctx is not None:
        return ctx.won_count

    return Match.all_objects.filter(
        participants__player=player,
        winner_side=F('participants__side'),
        is_confirmed=True,
    ).count()


def _opponents_in(player, match):
    """Players on the other side, read from prefetched participants."""
    side = _player_side(player, match)
    if side is None:
        return []
    return [p.player for p in match.participants.all() if p.side != side]


def _player_side(player, match):
    """Which side of the match the player is on, or None if not in it."""
    for participant in match.participants.all():
        if participant.player_id == player.pk:
            return participant.side
    return None


def _opponent_side(player, match):
    side = _player_side(player, match)
    if side is None:
        return None
    return Side.TWO if side == Side.ONE else Side.ONE


def _player_won_match(player, match):
    """Did the player's side win this match?"""
    if match.winner_side is None:
        return False
    return match.winner_side == _player_side(player, match)


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------

@register_checker
def check_first_blood(player, match, ctx=None):
    ctx = ctx or PlayerContext(player, match)
    if not _player_won_match(player, match):
        return []
    won_count = _player_won_count(player, ctx)
    if won_count >= 1:
        return ['first_blood']
    return []


@register_checker
def check_matches_played(player, match, ctx=None):
    ctx = ctx or PlayerContext(player, match)
    total = len(ctx.confirmed_matches)
    slugs = []
    if total >= 10:
        slugs.append('matches_played_bronze')
    if total >= 50:
        slugs.append('matches_played_silver')
    if total >= 100:
        slugs.append('matches_played_gold')
    return slugs


@register_checker
def check_matches_won(player, match, ctx=None):
    ctx = ctx or PlayerContext(player, match)
    won = _player_won_count(player, ctx)
    slugs = []
    if won >= 10:
        slugs.append('matches_won_bronze')
    if won >= 50:
        slugs.append('matches_won_silver')
    if won >= 100:
        slugs.append('matches_won_gold')
    return slugs


@register_checker
def check_win_streak(player, match, ctx=None):
    """Check current win streak across all confirmed matches."""
    ctx = ctx or PlayerContext(player, match)
    matches = ctx.confirmed_matches
    streak = 0
    for m in matches:
        if _player_won_match(player, m):
            streak += 1
        else:
            break

    slugs = []
    if streak >= 3:
        slugs.append('win_streak_bronze')
    if streak >= 5:
        slugs.append('win_streak_silver')
    if streak >= 10:
        slugs.append('win_streak_gold')
    return slugs


@register_checker
def check_perfect_game(player, match, ctx=None):
    """Perfect Game: win a game 11-0 in this match."""
    ctx = ctx or PlayerContext(player, match)
    side = _player_side(player, match)
    for game in match.games.all():
        if game.winner_side == side:
            if game.team1_score == 0 or game.team2_score == 0:
                return ['perfect_game']
    return []


@register_checker
def check_comeback_king(player, match, ctx=None):
    """Win after being down 0-2 in games."""
    ctx = ctx or PlayerContext(player, match)
    if not _player_won_match(player, match):
        return []
    if match.best_of < 5:
        return []

    side = _player_side(player, match)
    games = list(match.games.all())
    if len(games) < 3:
        return []

    # First two games must be losses for the player's side
    if games[0].winner_side == side or games[1].winner_side == side:
        return []

    return ['comeback_king']


@register_checker
def check_iron_wall(player, match, ctx=None):
    """Win a match conceding fewer than 20 total points."""
    ctx = ctx or PlayerContext(player, match)
    if not _player_won_match(player, match):
        return []

    side = _player_side(player, match)
    total_conceded = 0
    for game in match.games.all():
        if side == Side.ONE:
            total_conceded += game.team2_score
        else:
            total_conceded += game.team1_score

    if total_conceded < 20:
        return ['iron_wall']
    return []


@register_checker
def check_marathon_match(player, match, ctx=None):
    """Win a match that goes to the maximum number of games."""
    ctx = ctx or PlayerContext(player, match)
    if not _player_won_match(player, match):
        return []

    total_games = len(match.games.all())
    if total_games == match.best_of:
        return ['marathon_match']
    return []


@register_checker
def check_deuce_master(player, match, ctx=None):
    """Win games that went to deuce (both scores >= 10)."""
    ctx = ctx or PlayerContext(player, match)
    all_matches = ctx.confirmed_matches
    deuce_wins = 0

    for m in all_matches:
        side = _player_side(player, m)
        for game in m.games.all():
            if (game.team1_score >= 10 and game.team2_score >= 10
                    and game.winner_side == side):
                deuce_wins += 1

    slugs = []
    if deuce_wins >= 3:
        slugs.append('deuce_master_bronze')
    if deuce_wins >= 10:
        slugs.append('deuce_master_silver')
    if deuce_wins >= 25:
        slugs.append('deuce_master_gold')
    return slugs


@register_checker
def check_giant_slayer(player, match, ctx=None):
    """Beat someone rated significantly higher (uses pre-match Elo)."""
    ctx = ctx or PlayerContext(player, match)
    if not _player_won_match(player, match):
        return []

    elo = ctx.elo_by_player
    player_history = elo.get(player.pk)
    if player_history is None:
        return []

    opponent_ratings = [
        elo[opponent.pk].old_rating
        for opponent in _opponents_in(player, match)
        if opponent.pk in elo
    ]
    if not opponent_ratings:
        return []

    avg_opponent_rating = sum(opponent_ratings) / len(opponent_ratings)
    gap = avg_opponent_rating - player_history.old_rating

    slugs = []
    if gap >= 100:
        slugs.append('giant_slayer_bronze')
    if gap >= 200:
        slugs.append('giant_slayer_silver')
    if gap >= 300:
        slugs.append('giant_slayer_gold')
    return slugs


@register_checker
def check_rivalry(player, match, ctx=None):
    """Play the same opponent many times (singles only)."""
    ctx = ctx or PlayerContext(player, match)
    if match.is_double:
        return []

    opponent_counts = Counter()

    # Filtered and resolved in memory. Asking the database for each historical
    # opponent was the term that made this grow with the player's whole career.
    for m in ctx.confirmed_matches:
        if m.is_double:
            continue
        opponents = _opponents_in(player, m)
        if opponents:
            opponent_counts[opponents[0].pk] += 1

    if not opponent_counts:
        return []

    max_count = max(opponent_counts.values())
    slugs = []
    if max_count >= 5:
        slugs.append('rivalry_bronze')
    if max_count >= 10:
        slugs.append('rivalry_silver')
    if max_count >= 20:
        slugs.append('rivalry_gold')
    return slugs


@register_checker
def check_peak_performer(player, match, ctx=None):
    """Reach a new personal Elo peak from this match."""
    ctx = ctx or PlayerContext(player, match)
    history = ctx.elo_by_player.get(player.pk)
    if history is None:
        return []

    if history.new_rating >= player.elo_peak and history.rating_change > 0:
        return ['peak_performer']
    return []


# ---------------------------------------------------------------------------
# Progress calculation for template display
# ---------------------------------------------------------------------------

def get_achievement_progress(player):
    """Return achievement progress data for the player detail template.

    Returns a list of dicts grouped by achievement group, with earned status
    and current progress toward each threshold.
    """
    all_achievements = Achievement.objects.all()
    earned_map = {
        pa.achievement_id: pa
        for pa in PlayerAchievement.objects.filter(player=player).select_related('achievement')
    }

    # Pre-compute progress values
    confirmed = _player_confirmed_matches(player)
    confirmed_list = list(confirmed)

    total_played = len(confirmed_list)
    total_won = sum(1 for m in confirmed_list if _player_won_match(player, m))

    # Win streak (longest in history)
    best_streak = 0
    current = 0
    for m in reversed(confirmed_list):  # oldest first
        if _player_won_match(player, m):
            current += 1
            best_streak = max(best_streak, current)
        else:
            current = 0

    # Deuce wins count
    deuce_wins = 0
    for m in confirmed_list:
        side = _player_side(player, m)
        for game in m.games.all():
            if (game.team1_score >= 10 and game.team2_score >= 10
                    and game.winner_side == side):
                deuce_wins += 1

    # Giant slayer max gap
    max_elo_gap = 0
    for eh in EloHistory.objects.filter(player=player).select_related('match'):
        m = eh.match
        if not _player_won_match(player, m):
            continue
        opp_ratings = list(
            EloHistory.objects.filter(
                match=m, player__in=m.players_on(_opponent_side(player, m))
            ).values_list('old_rating', flat=True)
        )
        if opp_ratings:
            avg_opp = sum(opp_ratings) / len(opp_ratings)
            gap = avg_opp - eh.old_rating
            max_elo_gap = max(max_elo_gap, gap)

    # Rivalry max
    opponent_counts = Counter()
    for m in confirmed_list:
        if m.is_double:
            continue
        opp = m.players_on(_opponent_side(player, m)).first()
        if opp:
            opponent_counts[opp.pk] += 1
    max_rivalry = max(opponent_counts.values()) if opponent_counts else 0

    progress_map = {
        'matches_played': total_played,
        'matches_won': total_won,
        'win_streak': best_streak,
        'deuce_master': deuce_wins,
        'giant_slayer': int(max_elo_gap),
        'rivalry': max_rivalry,
    }

    # Build full list per achievement
    all_items = []
    for ach in all_achievements:
        earned_pa = earned_map.get(ach.id)
        raw_progress = progress_map.get(ach.group, 1 if earned_pa else 0)
        all_items.append({
            'achievement': ach,
            'earned': earned_pa is not None,
            'awarded_at': earned_pa.awarded_at if earned_pa else None,
            'progress': raw_progress,
            'threshold': ach.threshold,
        })

    # Collapse tiered groups: show highest earned + next unearned (if any).
    # One-off achievements (tier='none') pass through as-is.
    from itertools import groupby
    results = []
    for group, items in groupby(all_items, key=lambda x: x['achievement'].group):
        items = list(items)
        if items[0]['achievement'].tier == 'none':
            # One-off: just include it
            results.append(items[0])
            continue

        # Tiered: find highest earned and next unearned
        highest_earned = None
        next_unearned = None
        for item in items:  # already sorted by sort_order (from model Meta)
            if item['earned']:
                highest_earned = item
            elif next_unearned is None:
                next_unearned = item

        if highest_earned:
            results.append(highest_earned)
        if next_unearned:
            # Tag with the earned tier so template can color the icon
            next_unearned['earned_tier'] = highest_earned['achievement'].tier if highest_earned else None
            results.append(next_unearned)
        elif not highest_earned:
            # None earned at all — show the bronze tier
            results.append(items[0])

    return results

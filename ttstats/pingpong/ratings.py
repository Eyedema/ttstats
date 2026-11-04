"""Reading format and season ratings for display (KAN-48 / S4.3, KAN-50 / S4.4).

`elo.py` owns the arithmetic and the writes; this module owns the *reads* the
UI makes of `PlayerRating` and `RatingChange` once the `seasons` flag is on.
Every surface that shows a format rating goes through here, so the
provisional/unrated decision and the "which scope does this viewer see"
decision exist exactly once.

* **Unrated is not 1500.** A player with no row in a scope has no rating in it.
  Showing the seed value would make "has not played" look like "is average".
* **Provisional reads `elo.PROVISIONAL_MATCHES` at call time**, against the
  player's *all-time* match count in the format -- the same experience the
  K-factor uses, so a new season never makes an established player
  provisional again. Never re-type the threshold.
* **The preview scope is the scope that will be written.** A pending match is
  previewed in its own season (the current one, for anything played this
  season) and format; a match outside every season in the all-time format
  scope -- which is all `update_player_elo` writes for it.
"""
from datetime import timedelta

from django.db.models import Count, Exists, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import elo, flags
from . import seasons as season_rules
from .models import (
    MatchParticipant,
    Player,
    PlayerRating,
    RatingChange,
    RatingFormat,
    Season,
)

FLAG = "seasons"

UNRATED = "unrated"
PROVISIONAL = "provisional"
ESTABLISHED = "established"

# The leaderboard's `season` parameter for the all-time table.
ALL_TIME = "all"

MOVEMENT_WINDOW_DAYS = 7


def enabled(user):
    return flags.is_enabled(FLAG, user)


def rating_status(rated, experience):
    """UNRATED, PROVISIONAL or ESTABLISHED. `experience` is the all-time count."""
    if not rated:
        return UNRATED
    if (experience or 0) < elo.PROVISIONAL_MATCHES:
        return PROVISIONAL
    return ESTABLISHED


def format_label(fmt):
    return RatingFormat(fmt).label


def scope_label(scope):
    """'Singles · Autumn 2026', or 'Doubles · all-time'."""
    where = scope.season.name if scope.season is not None else "all-time"
    return f"{format_label(scope.format)} · {where}"


# ---------------------------------------------------------------------------
# Which scope
# ---------------------------------------------------------------------------


def club_seasons(club):
    """The active club's seasons, newest first. Explicitly filtered by club:
    with the `clubs` flag off `Season.objects` would return every club's."""
    if club is None:
        return []
    return list(Season.all_objects.filter(club=club).order_by("-starts_on"))


def current_season(seasons_list, today=None):
    """The season containing today (UTC), from an already-loaded list."""
    return season_rules.resolve_season(today or timezone.now(), seasons_list)


def display_scope(match):
    """The scope a match's rating change is shown in: its season and format.

    For a match played this season that is the viewer's current-season table.
    It is also exactly the scope `update_player_elo` writes, which is what
    lets the preview and the outcome stay one call.
    """
    return elo.RatingScope(
        Season.for_day(match.club_id, match.date_played), elo.match_format(match)
    )


def applied_changes(match, scope):
    """{player_id: rating_change} already written for `match` in `scope`."""
    qs = RatingChange.objects.filter(match=match, rating__format=scope.format)
    if scope.season_id is None:
        qs = qs.filter(rating__season__isnull=True)
    else:
        qs = qs.filter(rating__season_id=scope.season_id)
    return dict(qs.order_by().values_list("player_id", "rating_change"))


def projected_change_for(match, player, scope):
    """What `match` is worth to `player` in `scope`, signed; 0 if not in it."""
    return elo.projected_elo_change_for(match, player, scope)


# ---------------------------------------------------------------------------
# The leaderboard
# ---------------------------------------------------------------------------


def _scope_filter(qs, scope, prefix=""):
    qs = qs.filter(**{f"{prefix}format": scope.format})
    if scope.season_id is None:
        return qs.filter(**{f"{prefix}season__isnull": True})
    return qs.filter(**{f"{prefix}season_id": scope.season_id})


def leaderboard(scope, top_x):
    """(rows, unrated_players) for one scope. A constant number of queries.

    The aggregation is in the database: one query for the rating rows (with
    each player's all-time experience as a subquery), one for wins and weekly
    movement from `RatingChange`, one for the unrated. The legacy table's
    load-every-match-into-Python pattern is not repeated here.

    Rows carry `experience` rather than a status, so the provisional decision
    is made at presentation time and follows `elo.PROVISIONAL_MATCHES` on a
    cache hit as well as a miss.
    """
    experience = PlayerRating.all_objects.filter(
        player=OuterRef("player"), season__isnull=True, format=scope.format
    ).values("matches")[:1]
    # The club-scoped manager: one club's table (no-op with `clubs` off).
    rated = list(
        _scope_filter(PlayerRating.objects.all(), scope)
        .select_related("player")
        .annotate(experience=Coalesce(Subquery(experience), 0))
    )

    won = Exists(
        MatchParticipant.objects.filter(
            match_id=OuterRef("match_id"),
            player_id=OuterRef("player_id"),
            side=OuterRef("match__winner_side"),
        )
    )
    since = timezone.now() - timedelta(days=MOVEMENT_WINDOW_DAYS)
    tallies = {
        t["rating_id"]: t
        for t in RatingChange.objects.filter(rating_id__in=[r.pk for r in rated])
        .order_by()
        .values("rating_id")
        .annotate(
            wins=Count("pk", filter=Q(won)),
            movement=Coalesce(Sum("rating_change", filter=Q(created_at__gte=since)), 0),
        )
    }

    rows = []
    for r in rated:
        tally = tallies.get(r.pk, {})
        wins = tally.get("wins", 0)
        rows.append({
            "player": r.player,
            "elo_rating": r.rating,
            "elo_peak": r.peak,
            "total_matches": r.matches,
            "wins": wins,
            "losses": r.matches - wins,
            "win_rate": (wins / r.matches * 100) if r.matches else 0,
            "experience": r.experience,
            "movement": tally.get("movement", 0),
        })
    rows.sort(key=lambda x: (x["elo_rating"], x["wins"], x["win_rate"]), reverse=True)

    # Players who have played (anything, anywhere in the club) but have no
    # row in this scope: the first days of a season, or the other format.
    unrated = Player.objects.filter(
        Exists(PlayerRating.all_objects.filter(player=OuterRef("pk")))
    ).exclude(pk__in=[r.player_id for r in rated])
    if scope.season is not None:
        unrated = unrated.filter(club_id=scope.season.club_id)
    unrated = list(unrated.order_by("name"))

    return rows[:top_x], unrated


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def player_format_ratings(player):
    """Per format: this season's and the all-time rating, each with a status.

    The season is the current one of the player's own club. One query for the
    rows; `all_objects` is transitively scoped by the player, who came through
    the club-scoped manager.
    """
    season = Season.for_day(player.club_id, timezone.now())
    scope_q = Q(season__isnull=True)
    if season is not None:
        scope_q |= Q(season=season)
    rows = {
        (r.season_id, r.format): r
        for r in PlayerRating.all_objects.filter(scope_q, player=player)
    }

    formats = []
    for fmt in RatingFormat.values:
        all_time = rows.get((None, fmt))
        in_season = rows.get((season.pk, fmt)) if season is not None else None
        experience = all_time.matches if all_time else 0
        cells = {
            "season": _cell(in_season, experience),
            "all_time": _cell(all_time, experience),
        }
        # `columns` is the display order, so the template loops once.
        formats.append({
            "format": fmt, "label": format_label(fmt), **cells,
            "columns": [cells["season"], cells["all_time"]],
        })
    return {"season": season, "formats": formats}


def _cell(row, experience):
    return {
        "rating": row.rating if row is not None else None,
        "status": rating_status(row is not None, experience),
    }

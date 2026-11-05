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

from django.core.cache import cache
from django.db.models import Count, Exists, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import cache_utils, elo, flags, glicko2, glicko_adapter
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
GLICKO_FLAG = "glicko2"

# The two rating systems a page can show (KAN-72). Every read in this module
# takes one as a required keyword, so a forgotten call site raises instead of
# quietly showing Elo.
ELO = "elo"
GLICKO = "glicko2"

UNRATED = "unrated"
PROVISIONAL = "provisional"
ESTABLISHED = "established"

# The leaderboard's `season` parameter for the all-time table.
ALL_TIME = "all"

MOVEMENT_WINDOW_DAYS = 7


def enabled(user):
    return flags.is_enabled(FLAG, user)


def system(user):
    """The rating system `user` sees: GLICKO only with `seasons` AND `glicko2` on.

    `user=None` (jobs, commands) reads the settings values. There are only
    three states: today's Elo, seasons + Elo, and seasons + Glicko-2.
    """
    if flags.is_enabled(FLAG, user) and flags.is_enabled(GLICKO_FLAG, user):
        return GLICKO
    return ELO


class RatingStatus(str):
    """A status string ('unrated' | 'provisional' | 'established') with a `reason`.

    Compares equal to the plain string, so `status == ratings.PROVISIONAL`
    works. `reason` is None except for a provisional Glicko rating: 'new' (few
    matches) or 'returning' (RD high because of idle time).
    """

    reason = None

    def __new__(cls, value, reason=None):
        obj = super().__new__(cls, value)
        obj.reason = reason
        return obj


def _field(row, name):
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def display_rd(row, today=None, params=glicko_adapter.DEFAULT_PARAMS):
    """The RD to *show*: the stored one grown by the idle days up to `today`.

    `row` is a PlayerRating or a leaderboard row dict (anything with `rd`,
    `volatility` and `last_rated_on`). Display-only, and the one place that
    reads "now": the preview and the writes never do. None when unrated.
    """
    rd = _field(row, "rd")
    if rd is None:
        return None
    volatility = _field(row, "volatility") or glicko2.DEFAULT_VOLATILITY
    today = today or timezone.now().date()
    periods = glicko_adapter.days_to_periods(
        _field(row, "last_rated_on"), today, params
    )
    stored = glicko2.Rating(
        _field(row, "glicko_rating") or glicko2.BASE_RATING, rd, volatility
    )
    return glicko2.inflate(stored, periods, rd_max=params.rd_max).rd


def rating_status(rated, *, experience=None, rd=None, system):
    """UNRATED, PROVISIONAL or ESTABLISHED, as a `RatingStatus` carrying a reason.

    Elo: the all-time `experience` against `elo.PROVISIONAL_MATCHES`.
    Glicko: the *display* `rd` against `glicko_adapter.PROVISIONAL_RD`. Both
    thresholds are read at call time, so a cached table follows a change.
    """
    if not rated:
        return RatingStatus(UNRATED)
    if system == GLICKO:
        if rd is not None and rd > glicko_adapter.PROVISIONAL_RD:
            few = experience is None or experience < elo.PROVISIONAL_MATCHES
            return RatingStatus(PROVISIONAL, "new" if few else "returning")
        return RatingStatus(ESTABLISHED)
    if (experience or 0) < elo.PROVISIONAL_MATCHES:
        return RatingStatus(PROVISIONAL)
    return RatingStatus(ESTABLISHED)


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


def scope_for(club_id, day, is_double):
    """The scope a pairing on `day` is rated in: that day's season (else all-time)."""
    fmt = elo.DOUBLES if is_double else elo.SINGLES
    return elo.RatingScope(Season.for_day(club_id, day), fmt)


def _applied_rows(match, scope):
    qs = RatingChange.objects.filter(match=match, rating__format=scope.format)
    if scope.season_id is None:
        qs = qs.filter(rating__season__isnull=True)
    else:
        qs = qs.filter(rating__season_id=scope.season_id)
    return qs.order_by()


def applied_changes(match, scope, *, system):
    """{player_id: signed change} already written for `match` in `scope`.

    Glicko: a NULL `glicko_change` (the projection was skipped) reads 0 but the
    player stays in the dict, so "applied" still means applied.
    """
    qs = _applied_rows(match, scope)
    if system == GLICKO:
        return {
            pid: change or 0
            for pid, change in qs.values_list("player_id", "glicko_change")
        }
    return dict(qs.values_list("player_id", "rating_change"))


def applied_glicko_readings(match, scope):
    """{player_id: (glicko_new_rating, glicko_change)} written for `match`; NULLs dropped."""
    return {
        pid: (new, change)
        for pid, new, change in _applied_rows(match, scope)
        .exclude(glicko_new_rating__isnull=True)
        .values_list("player_id", "glicko_new_rating", "glicko_change")
    }


def glicko_movement(match, scope, player):
    """(old, new) Glicko rating of `player` across `match` in `scope`, or None."""
    return (
        _applied_rows(match, scope)
        .filter(
            player=player,
            glicko_old_rating__isnull=False,
            glicko_new_rating__isnull=False,
        )
        .values_list("glicko_old_rating", "glicko_new_rating")
        .first()
    )


def players_between(scope, climber, old_rating, new_rating):
    """Players with an account whose Glicko rating in `scope` is strictly between the two."""
    if new_rating <= old_rating:
        return Player.objects.none()
    in_between = PlayerRating.objects.filter(
        format=scope.format,
        season_id=scope.season_id,
        glicko_rating__gt=old_rating,
        glicko_rating__lt=new_rating,
        player__user__isnull=False,
    ).exclude(player=climber)
    return Player.objects.filter(pk__in=in_between.values("player_id"))


def _guarded_projection(match, scope):
    """The Glicko projection through the SAME guard the write uses, or None.

    `elo._plan_glicko` swallows any failure and rejects non-finite results and
    RD <= 0, so the preview is None exactly when the write stores NULLs.
    """
    book = elo.LiveBook(match, scope.season)
    return elo._plan_glicko(match, scope, book, glicko_adapter.DEFAULT_PARAMS)


def projected_change_for(match, player, scope, *, system):
    """What `match` is worth to `player` in `scope`, signed; 0 if not in it."""
    if system != GLICKO:
        return elo.projected_elo_change_for(match, player, scope)
    if player is None or scope is elo.LEGACY:
        return 0
    projection = _guarded_projection(match, scope)
    if projection is None:
        return 0
    for update in projection.updates:
        if update.player_id == player.pk:
            return glicko_adapter.history_values(update)["glicko_change"]
    return 0


def projected_changes_by_player(match, scope, *, system):
    """({player_id: signed change}, is_pending) for a Glicko match; ({}, True) if none.

    Doubles are rated per player, so the honest preview is one figure per
    player, not the side mean. Applied: each player's stored `glicko_change`
    (NULL reads 0). Pending: the same guarded projection confirmation writes,
    through `history_values`, so each figure equals what will be stored.
    Elo has no per-player split: it returns ({}, False).
    """
    if system != GLICKO:
        return {}, False
    applied = applied_changes(match, scope, system=system)
    if applied:
        return applied, False
    projection = _guarded_projection(match, scope)
    if projection is None:
        return {}, True
    return (
        {
            update.player_id: glicko_adapter.history_values(update)["glicko_change"]
            for update in projection.updates
        },
        True,
    )


def match_deltas(match, scope, *, system):
    """(side1_delta, side2_delta, is_pending) for a match in `scope`.

    Applied: the recorded `RatingChange`. Pending: the projection, the same
    arithmetic confirmation will write. A Glicko side delta is the rounded mean
    movement of its players, as `GlickoProjection.side*_change`.
    """
    side1, side2 = match.sides()
    if system != GLICKO:
        applied = applied_changes(match, scope, system=system)
        if applied:
            return (
                next((applied[p.pk] for p in side1 if p.pk in applied), 0),
                next((applied[p.pk] for p in side2 if p.pk in applied), 0),
                False,
            )
        projection = elo.projected_elo_changes(match, scope)
        return projection.side1_change, projection.side2_change, True

    rows = list(
        _applied_rows(match, scope).values_list(
            "player_id", "glicko_old_rating", "glicko_new_rating"
        )
    )
    if rows:
        by_player = {pid: (old, new) for pid, old, new in rows}

        def side_delta(players):
            known = [
                by_player[p.pk]
                for p in players
                if p.pk in by_player and None not in by_player[p.pk]
            ]
            if not known:
                return 0
            n = len(known)
            return glicko_adapter._rounded_change(
                sum(new for _, new in known) / n, sum(old for old, _ in known) / n
            )

        return side_delta(side1), side_delta(side2), False
    projection = _guarded_projection(match, scope)
    if projection is None:
        return 0, 0, True
    return projection.side1_change, projection.side2_change, True


def glicko_readings(players, *, scope, day=None):
    """{player_id: (rating, rd)} rounded, as `win_probability` reads them today."""
    players = list(players)
    day = day or timezone.now().date()
    book = elo.LiveBook.for_players([p.pk for p in players], scope.season, scope.format)
    out = {}
    for p in players:
        reading = glicko_adapter.glicko_reading(
            book, p.pk, scope, day, glicko_adapter.DEFAULT_PARAMS
        )
        out[p.pk] = (round(reading.rating), round(reading.rd))
    return out


def win_probability(side1, side2, *, system, scope, match=None, day=None):
    """(side1_pct, side2_pct) as integer percentages, or (None, None) if Glicko-2 fails.

    Elo: `elo.get_win_probability`. Glicko: each side is a composite player
    (`composite_opponent`: mean rating, RMS RD) fed to `glicko2.win_probability`.
    A confirmed `match` uses the stored pre-match `glicko_old_rating` and
    `glicko_rd_before`; otherwise the reading is `glicko_reading` on the match's
    day (or `day`, or today), through `LiveBook.for_players`.
    """
    side1, side2 = list(side1), list(side2)
    if system != GLICKO:
        return elo.get_win_probability(side1, side2, match=match)
    if not side1 or not side2:
        return (50, 50)

    stored = {}
    if match is not None and getattr(match, "pk", None) is not None:
        stored = {
            pid: glicko2.Rating(old, rd, glicko2.DEFAULT_VOLATILITY)
            for pid, old, rd in _applied_rows(match, scope)
            .exclude(glicko_old_rating__isnull=True)
            .exclude(glicko_rd_before__isnull=True)
            .values_list("player_id", "glicko_old_rating", "glicko_rd_before")
        }
    if day is None:
        day = getattr(match, "date_played", None) or timezone.now()
    day = day.date() if hasattr(day, "hour") else day

    missing = [p.pk for p in side1 + side2 if p.pk not in stored]
    if missing:
        book = elo.LiveBook.for_players(missing, scope.season, scope.format)
        for pid in missing:
            stored[pid] = glicko_adapter.glicko_reading(
                book, pid, scope, day, glicko_adapter.DEFAULT_PARAMS
            )

    r1, rd1 = glicko_adapter.composite_opponent(stored[p.pk] for p in side1)
    r2, rd2 = glicko_adapter.composite_opponent(stored[p.pk] for p in side2)
    try:
        pct = round(glicko2.win_probability(r1, rd1, r2, rd2) * 100)
    except glicko2.Glicko2Error:
        return (None, None)  # the bar is hidden, never a 500; callers unpack a pair
    return (pct, 100 - pct)


# ---------------------------------------------------------------------------
# The leaderboard
# ---------------------------------------------------------------------------


def _scope_filter(qs, scope, prefix=""):
    qs = qs.filter(**{f"{prefix}format": scope.format})
    if scope.season_id is None:
        return qs.filter(**{f"{prefix}season__isnull": True})
    return qs.filter(**{f"{prefix}season_id": scope.season_id})


def leaderboard(scope, top_x, *, system):
    """(rows, unrated_players) for one scope. A constant number of queries.

    The aggregation is in the database: one query for the rating rows (with
    each player's all-time experience as a subquery), one for wins and weekly
    movement from `RatingChange`, one for the unrated. The legacy table's
    load-every-match-into-Python pattern is not repeated here.

    Rows carry `experience` rather than a status, so the provisional decision
    is made at presentation time and follows `elo.PROVISIONAL_MATCHES` on a
    cache hit as well as a miss. Every row has the neutral keys `rating`,
    `rd`, `peak`, `last_rated_on` (Elo rows: `rd` None); Elo rows keep their
    `elo_rating` / `elo_peak` aliases.

    Glicko: sorted by `glicko_rating`; a row with a NULL one is *unrated* and
    goes to the second list. `rd` is the stored RD (the presenter inflates it
    to today), `movement` is `Sum(glicko_change)`, `peak` `Max(glicko_new_rating)`.
    """
    glicko = system == GLICKO
    experience = PlayerRating.all_objects.filter(
        player=OuterRef("player"), season__isnull=True, format=scope.format
    ).values("matches")[:1]
    # The club-scoped manager: one club's table (no-op with `clubs` off).
    rated_qs = _scope_filter(PlayerRating.objects.all(), scope)
    if glicko:
        rated_qs = rated_qs.filter(glicko_rating__isnull=False)
    rated = list(
        rated_qs.select_related("player").annotate(
            experience=Coalesce(Subquery(experience), 0)
        )
    )

    won = Exists(
        MatchParticipant.objects.filter(
            match_id=OuterRef("match_id"),
            player_id=OuterRef("player_id"),
            side=OuterRef("match__winner_side"),
        )
    )
    since = timezone.now() - timedelta(days=MOVEMENT_WINDOW_DAYS)
    moved = "glicko_change" if glicko else "rating_change"
    extra = {"top": Max("glicko_new_rating")} if glicko else {}
    tallies = {
        t["rating_id"]: t
        for t in RatingChange.objects.filter(rating_id__in=[r.pk for r in rated])
        .order_by()
        .values("rating_id")
        .annotate(
            wins=Count("pk", filter=Q(won)),
            movement=Coalesce(Sum(moved, filter=Q(created_at__gte=since)), 0),
            **extra,
        )
    }

    rows = []
    for r in rated:
        tally = tallies.get(r.pk, {})
        wins = tally.get("wins", 0)
        row = {
            "player": r.player,
            "total_matches": r.matches,
            "wins": wins,
            "losses": r.matches - wins,
            "win_rate": (wins / r.matches * 100) if r.matches else 0,
            "experience": r.experience,
            "movement": tally.get("movement", 0),
        }
        if glicko:
            top = tally.get("top")
            row.update(
                {
                    "rating": round(r.glicko_rating),
                    "rd": r.rd,
                    "volatility": r.volatility,
                    "glicko_rating": r.glicko_rating,
                    "peak": round(
                        max(top, r.glicko_rating)
                        if top is not None
                        else r.glicko_rating
                    ),
                    "last_rated_on": r.last_rated_on,
                    "last_played": r.last_rated_on,
                    "_sort": r.glicko_rating,
                }
            )
        else:
            row.update(
                {
                    "rating": r.rating,
                    "rd": None,
                    "peak": r.peak,
                    "last_rated_on": None,
                    "elo_rating": r.rating,
                    "elo_peak": r.peak,
                    "_sort": r.rating,
                }
            )
        rows.append(row)
    rows.sort(key=lambda x: (x["_sort"], x["wins"], x["win_rate"]), reverse=True)
    for row in rows:
        del row["_sort"]

    # Players who have played (anything, anywhere in the club) but have no
    # row in this scope: the first days of a season, or the other format.
    # Under Glicko a row with NULL Glicko state counts as no rating.
    unrated = Player.objects.filter(
        Exists(PlayerRating.all_objects.filter(player=OuterRef("pk")))
    ).exclude(pk__in=[r.player_id for r in rated])
    if scope.season is not None:
        unrated = unrated.filter(club_id=scope.season.club_id)
    unrated = list(unrated.order_by("name"))

    return rows[:top_x], unrated


def rated_player_count(scope, *, system):
    """How many players have a rating in `scope` (the day-one fallback test)."""
    qs = _scope_filter(PlayerRating.objects.all(), scope)
    if system == GLICKO:
        qs = qs.filter(glicko_rating__isnull=False)
    return qs.count()


MIN_RATED_FOR_SEASON_TABLE = 2


def season_table_is_empty(scope, *, system):
    """True when a *season* scope has fewer than 2 rated players in its format.

    The default leaderboard scope then falls back to all-time (the day-one
    empty table).
    """
    if scope.season is None:
        return False
    return rated_player_count(scope, system=system) < MIN_RATED_FOR_SEASON_TABLE


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def player_format_ratings(player, *, system):
    """Per format: this season's and the all-time rating, each with a status.

    The season is the current one of the player's own club. One query for the
    rows (plus one for the Glicko peaks); `all_objects` is transitively scoped
    by the player, who came through the club-scoped manager. Cells carry
    `rating` and `status`; Glicko cells also `rd`, `reason`, `peak`.
    """
    glicko = system == GLICKO
    season = Season.for_day(player.club_id, timezone.now())
    scope_q = Q(season__isnull=True)
    if season is not None:
        scope_q |= Q(season=season)
    rows = {
        (r.season_id, r.format): r
        for r in PlayerRating.all_objects.filter(scope_q, player=player)
    }
    peaks = {}
    if glicko and rows:
        peaks = dict(
            RatingChange.objects.filter(rating_id__in=[r.pk for r in rows.values()])
            .order_by()
            .values_list("rating_id")
            .annotate(top=Max("glicko_new_rating"))
        )

    today = timezone.now().date()
    formats = []
    for fmt in RatingFormat.values:
        all_time = rows.get((None, fmt))
        in_season = rows.get((season.pk, fmt)) if season is not None else None
        experience = all_time.matches if all_time else 0
        cells = {
            "season": _cell(in_season, experience, system, peaks, today),
            "all_time": _cell(all_time, experience, system, peaks, today),
        }
        # `columns` is the display order, so the template loops once.
        formats.append(
            {
                "format": fmt,
                "label": format_label(fmt),
                **cells,
                "columns": [cells["season"], cells["all_time"]],
            }
        )
    return {"season": season, "formats": formats}


def _cell(row, experience, system, peaks, today):
    if system == GLICKO:
        has = row is not None and row.glicko_rating is not None
        shown = display_rd(row, today) if has else None
        # The verdict uses the rounded RD: the same figure the page shows as the ±.
        rd = round(shown) if shown is not None else None
        status = rating_status(has, experience=experience, rd=rd, system=system)
        top = peaks.get(row.pk) if row is not None else None
        return {
            "rating": round(row.glicko_rating) if has else None,
            "rd": rd,
            "last_played": row.last_rated_on if has else None,
            "peak": round(max(top, row.glicko_rating))
            if has and top is not None
            else (round(row.glicko_rating) if has else None),
            "status": status,
            "reason": status.reason,
        }
    return {
        "rating": row.rating if row is not None else None,
        "status": rating_status(row is not None, experience=experience, system=system),
    }


# ---------------------------------------------------------------------------
# The chart and the nav
# ---------------------------------------------------------------------------

HISTORY_TIMEOUT = 600
NAV_TIMEOUT = 300


def rating_history(player, *, system, fmt=elo.SINGLES):
    """The player's all-time rating series for the profile chart.

    `{labels, data, lower, upper}`; empty lists when they have no history.
    Elo reads `EloHistory` (combined, lifetime); Glicko the `RatingChange`
    Glicko columns of the all-time `fmt` row, with `lower`/`upper` the
    rating +/- the RD after each match. Each system has its own cache key.
    """
    from .models import EloHistory

    key = cache_utils.rating_history_key(player, system)
    cached = cache.get(key)
    if cached is not None:
        return cached

    if system == GLICKO:
        points = list(
            RatingChange.objects.filter(
                player=player,
                rating__format=fmt,
                rating__season__isnull=True,
                glicko_new_rating__isnull=False,
            )
            .order_by("created_at", "pk")
            .values_list("created_at", "glicko_new_rating", "glicko_rd_after")
        )
        if points:
            start_rd = glicko2.MAX_RD
            data = [glicko2.BASE_RATING] + [round(p[1]) for p in points]
            rds = [start_rd] + [p[2] or 0 for p in points]
            result = {
                "labels": ["Start"] + [p[0].strftime("%Y-%m-%d") for p in points],
                "data": data,
                "lower": [round(d - r) for d, r in zip(data, rds)],
                "upper": [round(d + r) for d, r in zip(data, rds)],
            }
        else:
            result = {"labels": [], "data": [], "lower": [], "upper": []}
    else:
        points = list(
            EloHistory.objects.filter(player=player)
            .order_by("created_at")
            .values_list("created_at", "new_rating")
        )
        result = {
            "labels": ["Start"] + [p[0].strftime("%Y-%m-%d") for p in points]
            if points
            else [],
            "data": [1500] + [p[1] for p in points] if points else [],
            "lower": [],
            "upper": [],
        }
    cache.set(key, result, HISTORY_TIMEOUT)
    return result


def _player_club_rows(model, player):
    """`model` rows of the PLAYER's club, whichever club the viewer has active.

    The nav key is namespaced by the player's club, so the rank must be counted
    there too, or a multi-club user caches club B's rank under club A's key.
    `_base_manager` is transitively scoped by the player's `club_id`
    (docs/decisions/kan-24-all-objects-audit.md). With `clubs` off the active-club
    manager is a no-op and stays as before.
    """
    from ttstats.middleware import get_current_user

    if not flags.is_enabled("clubs", get_current_user()):
        return model.objects.all()
    return model._base_manager.filter(club_id=player.club_id)


def nav_reading(player, *, system):
    """The nav's rating line: `{rating, rd, rank}` for the player.

    Elo: the lifetime `Player.elo_rating` and its rank among players (RD None).
    Glicko: the current-season singles rating (else all-time) with its display
    RD and its rank in that table. Cached under `player_rank_key(player, system)`;
    the Glicko entry holds the stored RD and `last_rated_on`, inflated on read.
    """
    key = cache_utils.player_rank_key(player, system)
    if system != GLICKO:
        rank = cache.get(key)
        if rank is None:
            rank = (
                _player_club_rows(Player, player).filter(
                    elo_rating__gt=player.elo_rating
                ).count()
                + 1
            )
            cache.set(key, rank, NAV_TIMEOUT)
        return {"rating": player.elo_rating, "rd": None, "rank": rank}

    cached = cache.get(key)
    if cached is None:
        season = Season.for_day(player.club_id, timezone.now())
        chosen = None
        for season_id in ([season.pk] if season is not None else []) + [None]:
            row = PlayerRating.all_objects.filter(
                player=player,
                format=elo.SINGLES,
                season_id=season_id,
                glicko_rating__isnull=False,
            ).first()
            if row is not None:
                chosen = row
                break
        if chosen is None:
            cached = {"rating": None}
        else:
            # The rank is among the PLAYER's club's rows in the same table.
            rank = (
                1
                + _player_club_rows(PlayerRating, player).filter(
                    format=elo.SINGLES,
                    season_id=chosen.season_id,
                    glicko_rating__gt=chosen.glicko_rating,
                ).count()
            )
            cached = {
                "rating": round(chosen.glicko_rating),
                "rd": chosen.rd,
                "volatility": chosen.volatility,
                "glicko_rating": chosen.glicko_rating,
                "last_rated_on": chosen.last_rated_on,
                "rank": rank,
            }
        cache.set(key, cached, NAV_TIMEOUT)
    if cached.get("rating") is None:
        return {"rating": None, "rd": None, "rank": None}
    shown = display_rd(cached)
    return {
        "rating": cached["rating"],
        "rd": round(shown) if shown is not None else None,
        "rank": cached["rank"],
    }

"""Rollups and caching over the point log (KAN-30 / S2.4).

The thin Django-touching layer above ``point_stats.py``. That module holds the
rules and stays pure; this one reads rows, writes ``MatchPointSummary``, and
owns the cache keys. Nothing here re-implements a statistic.

**Three layers, and the direction of trust runs one way:**

1. ``PointEvent`` -- the raw log, the source of truth, never derived.
2. ``MatchPointSummary`` -- a per-match, per-player rollup. A *cache*, written
   when a match completes and rebuildable from the raw log at any time by
   ``manage.py rebuild_point_rollups``.
3. Cache entries for the player-level and head-to-head folds, invalidated
   through the existing ``cache_utils`` hooks.

**Club scoping reaches through the row's own parents.** A ``Player`` or a
``Match`` handed to this module has already come through a club-scoped manager,
and a player belongs to exactly one club -- so filtering summaries by that
player is transitively scoped, the same argument the audit table makes for
``ChampionshipDetailView``. What is *not* transitive is the cache: every key
below carries ``cache_utils.club_prefix()``, because a correctly-scoped query
served from an entry another club warmed is still a leak.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import Max, Sum

from . import cache_utils, point_stats
from .models import MatchPointSummary, PointEvent, Side

logger = logging.getLogger(__name__)

# Matches the existing player_stats entry, which is invalidated by the same
# signal path. A different TTL here would only mean two figures on one page
# ageing out at different moments.
CACHE_TTL = 600


# ---------------------------------------------------------------------------
# Raw log -> pure statistics
# ---------------------------------------------------------------------------


def points_for_match(match) -> list[point_stats.Point]:
    """The match's point log as the plain tuples ``point_stats`` consumes.

    Ordered by ``sequence``, which ``PointEvent.Meta.ordering`` already
    guarantees -- calling ``.order_by()`` here would bypass any prefetch cache
    for no gain, the same trap ``achievements.py`` documents for ``Game``.
    """
    return [
        point_stats.Point(
            game_number=event.game_number,
            scoring_side=event.scoring_side,
            serving_side=event.serving_side,
            team1_score=event.team1_score,
            team2_score=event.team2_score,
        )
        for event in PointEvent.objects.filter(match=match)
    ]


def match_stats(match) -> point_stats.MatchPointStats:
    """Point statistics for one match, computed from the raw log."""
    return point_stats.match_stats(points_for_match(match))


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------


def _summary_fields(stats: point_stats.SideStats) -> dict:
    return {
        "serve_won": stats.serve.won,
        "serve_total": stats.serve.total,
        "receive_won": stats.receive.won,
        "receive_total": stats.receive.total,
        "points_won": stats.points.won,
        "points_total": stats.points.total,
        "games_won": stats.games.won,
        "games_total": stats.games.total,
        "deuce_won": stats.deuce.won,
        "deuce_total": stats.deuce.total,
        "comebacks": stats.comebacks,
        "largest_deficit_overcome": stats.largest_deficit_overcome,
        "longest_run": stats.longest_run,
    }


def side_stats_from_summary(summary) -> point_stats.SideStats:
    """A stored rollup row back into the pure module's vocabulary."""
    return point_stats.SideStats(
        side=summary.side,
        serve=point_stats.Rate(summary.serve_won, summary.serve_total),
        receive=point_stats.Rate(summary.receive_won, summary.receive_total),
        points=point_stats.Rate(summary.points_won, summary.points_total),
        games=point_stats.Rate(summary.games_won, summary.games_total),
        deuce=point_stats.Rate(summary.deuce_won, summary.deuce_total),
        comebacks=summary.comebacks,
        largest_deficit_overcome=summary.largest_deficit_overcome,
        longest_run=summary.longest_run,
    )


def rebuild_match_summaries(match) -> int:
    """Recompute this match's rollup rows from its raw log. Returns row count.

    Idempotent, and safe to run concurrently with itself: the values are a
    deterministic function of the log, and ``update_or_create`` against the
    ``(match, player)`` unique constraint means two racing rebuilds write the
    same numbers rather than duplicating rows. That is why this does not need
    the conditional-``.update()`` compare-and-set that
    ``Match.result_notified_at`` uses -- there is no "first writer wins" to
    decide, only one correct answer.

    A match with no point log has its rows removed rather than zeroed: a zero
    rollup and an absent one mean different things to every consuming surface,
    and "this match was not scored live" is the absent one.
    """
    stats = match_stats(match)

    with transaction.atomic():
        if not stats.has_data:
            deleted, _ = MatchPointSummary.objects.filter(match=match).delete()
            return 0

        side1, side2 = match.sides()
        written = 0
        for side, players in ((Side.ONE, side1), (Side.TWO, side2)):
            fields = _summary_fields(stats.side(int(side)))
            for player in players:
                MatchPointSummary.objects.update_or_create(
                    match=match,
                    player=player,
                    defaults={"side": side, **fields},
                )
                written += 1

        # A participant removed from the match since the last rebuild must not
        # keep a row: it would go on contributing to that player's career.
        keep = [p.pk for p in side1 + side2]
        MatchPointSummary.objects.filter(match=match).exclude(
            player_id__in=keep
        ).delete()

    invalidate_point_caches(match)
    return written


def invalidate_for_match(match) -> None:
    """Recompute this match's rollups if it has any, then drop the folds.

    Called wherever the log can change under a rollup that already exists.
    Cheap when there is nothing to do: one indexed existence check.
    """
    if not MatchPointSummary.objects.filter(match=match).exists():
        return
    rebuild_match_summaries(match)


def check_match(match) -> dict | None:
    """Compare a match's stored rollups against its raw log.

    Returns ``None`` when they agree, or a dict describing the disagreement.
    **Reports, never repairs and never raises** -- the same contract as the
    tenancy canary. A consistency check that mutates data on read is a much
    worse failure than the drift it found.
    """
    stored = {s.player_id: s for s in MatchPointSummary.objects.filter(match=match)}
    stats = match_stats(match)

    if not stats.has_data:
        if stored:
            logger.error(
                "Point rollup drift: match %s has %d summary rows but no point log",
                match.pk, len(stored),
            )
            return {"match": match.pk, "reason": "rollups without a log"}
        return None

    expected = {}
    side1, side2 = match.sides()
    for side, players in ((Side.ONE, side1), (Side.TWO, side2)):
        fields = _summary_fields(stats.side(int(side)))
        for player in players:
            expected[player.pk] = fields

    if set(expected) != set(stored):
        logger.error(
            "Point rollup drift: match %s expected rows for %s, found %s",
            match.pk, sorted(expected), sorted(stored),
        )
        return {"match": match.pk, "reason": "row set differs"}

    for player_id, fields in expected.items():
        row = stored[player_id]
        differing = {
            name: (value, getattr(row, name))
            for name, value in fields.items()
            if getattr(row, name) != value
        }
        if differing:
            logger.error(
                "Point rollup drift: match %s player %s differs on %s",
                match.pk, player_id, sorted(differing),
            )
            return {"match": match.pk, "player": player_id, "fields": differing}

    return None


# ---------------------------------------------------------------------------
# Player-level and head-to-head folds, cached
# ---------------------------------------------------------------------------

_AGGREGATES = {
    "serve_won": Sum("serve_won"),
    "serve_total": Sum("serve_total"),
    "receive_won": Sum("receive_won"),
    "receive_total": Sum("receive_total"),
    "points_won": Sum("points_won"),
    "points_total": Sum("points_total"),
    "games_won": Sum("games_won"),
    "games_total": Sum("games_total"),
    "deuce_won": Sum("deuce_won"),
    "deuce_total": Sum("deuce_total"),
    "comebacks": Sum("comebacks"),
    # Maxima, not sums -- see MatchPointSummary's docstring.
    "largest_deficit_overcome": Max("largest_deficit_overcome"),
    "longest_run": Max("longest_run"),
}


def _fold(queryset) -> dict:
    """One aggregate query folding many rollup rows into a career record."""
    row = queryset.aggregate(**_AGGREGATES)
    return {name: (row.get(name) or 0) for name in _AGGREGATES}


def _side_stats_from_fold(folded: dict) -> point_stats.SideStats:
    return point_stats.SideStats(
        serve=point_stats.Rate(folded["serve_won"], folded["serve_total"]),
        receive=point_stats.Rate(folded["receive_won"], folded["receive_total"]),
        points=point_stats.Rate(folded["points_won"], folded["points_total"]),
        games=point_stats.Rate(folded["games_won"], folded["games_total"]),
        deuce=point_stats.Rate(folded["deuce_won"], folded["deuce_total"]),
        comebacks=folded["comebacks"],
        largest_deficit_overcome=folded["largest_deficit_overcome"],
        longest_run=folded["longest_run"],
    )


# The keys live in cache_utils beside every other key builder, and NOT here.
# `invalidate_match_caches` / `invalidate_player_caches` are already called
# from the right places; a second, private copy of a key name is how a cache
# survives the invalidation meant to clear it.
player_cache_key = cache_utils.player_point_stats_key
head_to_head_cache_key = cache_utils.h2h_point_stats_key


def player_point_stats(player) -> point_stats.SideStats:
    """A player's career point record. One query on a miss, none on a hit.

    Folds the rollup rows rather than scanning the raw log: a season's play is
    a few hundred summary rows against tens of thousands of point rows.

    A player with nothing live-scored gets the zero state, which is cached too
    -- so the common case costs one query once and nothing thereafter. Callers
    decide whether to render anything by asking ``has_sample``; they must not
    render a section for a zero state (S2.6).
    """
    key = player_cache_key(player)
    folded = cache.get(key)
    if folded is None:
        folded = _fold(MatchPointSummary.objects.filter(player=player))
        cache.set(key, folded, CACHE_TTL)
    return _side_stats_from_fold(folded)


def head_to_head_point_stats(player1, player2, shared_match_ids) -> dict:
    """Both players' point records, restricted to the matches they shared.

    ``shared_match_ids`` is resolved by the caller because the caller already
    knows which matches those are. Restriction happens **at the query level**:
    folding a player's whole career and subtracting would leak their global
    rate into a head-to-head comparison, which is precisely the number the
    screen exists to deny.
    """
    key = head_to_head_cache_key(player1, player2)
    cached = cache.get(key)
    if cached is None:
        cached = {}
        for player in (player1, player2):
            cached[str(player.pk)] = _fold(
                MatchPointSummary.objects.filter(
                    player=player, match_id__in=shared_match_ids
                )
            )
        cache.set(key, cached, CACHE_TTL)

    return {
        player.pk: _side_stats_from_fold(cached[str(player.pk)])
        for player in (player1, player2)
    }


def invalidate_point_caches(match) -> None:
    """Drop the folds every player in this match contributes to.

    Deliberately keyed on the match's participants rather than flushing a
    namespace: a match changes exactly these players' careers and exactly one
    head-to-head pair.
    """
    side1, side2 = match.sides()
    keys = [player_cache_key(p) for p in side1 + side2]
    if len(side1) == 1 and len(side2) == 1:
        keys.append(head_to_head_cache_key(side1[0], side2[0]))
    cache.delete_many(keys)

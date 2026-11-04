"""
Elo rating calculation for TTStats.
Uses traditional Elo formula with table tennis-specific K-factor adjustments.
Supports both 1v1 and 2v2 matches.

**One arithmetic, several scopes** (KAN-9, docs/decisions/kan-9-seasons.md).
`projected_elo_changes(match, scope)` is the only place ratings are computed:

* `LEGACY` reads the `Player.elo_*` fields -- lifetime, singles and doubles
  combined -- exactly as before KAN-9. The parity harness pins it.
* `RatingScope(season, format)` reads `PlayerRating`: the all-time rating in
  that format when `season` is None, else the season rating, seeded on first
  use by `seasons.carry_over_seed` from the all-time rating. Experience (the
  provisional boost) is always the all-time match count in the format.

`update_player_elo` writes the legacy scope, the all-time format scope and the
match's season scope (if any) in one transaction.
"""

import logging
from typing import NamedTuple

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import glicko_adapter, seasons

logger = logging.getLogger(__name__)

# Standard chess K-factor. A named constant so the parity harness can inject a
# deliberate divergence (`elo_parity --inject-k-factor`) and prove it notices.
BASE_K = 32

# A player is provisional (K x 1.5) until this many rated matches.
PROVISIONAL_MATCHES = 20

SINGLES = "singles"
DOUBLES = "doubles"


class _Legacy:
    """The pre-KAN-9 scope: `Player.elo_rating` / `matches_for_elo`."""

    def __repr__(self):
        return "LEGACY"


LEGACY = _Legacy()


class RatingScope(NamedTuple):
    """Where a projection reads ratings from: a season (None = all-time) and a format."""

    season: object
    format: str

    @property
    def season_id(self):
        return getattr(self.season, "pk", self.season)


def match_format(match):
    return DOUBLES if match.is_double else SINGLES


def k_factor(match, matches_played, *, base_k=None):
    """
    K-factor from match importance and the player's experience.
    Returns higher K for:
    - Tournament matches (more important)
    - Longer matches (more reliable result)
    - New players (first PROVISIONAL_MATCHES matches)

    `base_k` overrides `BASE_K` for one call; only the parity harness passes
    it, to prove it detects a divergence.
    """
    if base_k is None:
        base_k = BASE_K

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

    # New player boost
    if matches_played < PROVISIONAL_MATCHES:
        experience_multiplier = 1.5
    else:
        experience_multiplier = 1.0

    k = base_k * type_multiplier * best_of_multiplier * experience_multiplier
    return k


def calculate_k_factor(match, player, *, base_k=None):
    """The legacy K: experience is `player.matches_for_elo`, both formats combined."""
    return k_factor(match, player.matches_for_elo, base_k=base_k)


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


def projected_elo_changes(match, scope=LEGACY, *, book=None, base_k=None):
    """An EloProjection for a match, calculated but not applied.

    The dashboard tells you what a pending confirmation costs -- "-16 Elo if
    true" -- before you agree to it, and the match detail shows the same figure
    next to each name. That number has to be the number that will actually be
    written when both players confirm, so this is the arithmetic
    `update_player_elo` uses rather than a second copy of it -- for every
    scope, and for the replay in `elo_replay`.

    `scope` is `LEGACY` (Player fields) or a `RatingScope`, read from `book`:
    a `RatingBook` (default: the database, via `LiveBook`). `base_k`
    overrides the base K (the parity harness's injected divergence only).

    Zero changes when the match has no winner or a side is empty; a match
    nobody has won yet moves nobody's rating.
    """
    from .models import Side

    nothing = EloProjection(0, 0, 0.0, 0.0)

    if not match.winner_side:
        return nothing

    # One query for both sides, memoized on the match -- update_player_elo
    # calls this and then needs the same two lists itself.
    team1_players, team2_players = match.sides()
    if not team1_players or not team2_players:
        return nothing

    if scope is LEGACY:
        r1 = sum(p.elo_rating for p in team1_players) / len(team1_players)
        r2 = sum(p.elo_rating for p in team2_players) / len(team2_players)
        k1_list = [calculate_k_factor(match, p, base_k=base_k) for p in team1_players]
        k2_list = [calculate_k_factor(match, p, base_k=base_k) for p in team2_players]
    else:
        if book is None:
            book = LiveBook(match, scope.season)
        read1 = [scope_reading(book, p.pk, scope) for p in team1_players]
        read2 = [scope_reading(book, p.pk, scope) for p in team2_players]
        r1 = sum(r for r, _ in read1) / len(read1)
        r2 = sum(r for r, _ in read2) / len(read2)
        k1_list = [k_factor(match, n, base_k=base_k) for _, n in read1]
        k2_list = [k_factor(match, n, base_k=base_k) for _, n in read2]

    s1 = 1 if match.winner_side == Side.ONE else 0
    s2 = 1 - s1

    # For 2v2 the team K-factor is the average of its members', which
    # preserves the new-player boost inside a team.
    k1_team = sum(k1_list) / len(k1_list)
    k2_team = sum(k2_list) / len(k2_list)

    return EloProjection(
        calculate_elo_change(r1, r2, s1, k1_team),
        calculate_elo_change(r2, r1, s2, k2_team),
        k1_team,
        k2_team,
    )


# ---------------------------------------------------------------------------
# Rating books: where a format scope's ratings are read from and written to.
# A book maps (player_id, season_id, format) to a row with `rating`, `peak`
# and `matches` -- a PlayerRating (LiveBook) or a RatingState (replay).
# ---------------------------------------------------------------------------


class RatingState:
    """An in-memory rating row, shaped like PlayerRating."""

    __slots__ = (
        "player_id", "club_id", "season_id", "format", "rating", "peak", "matches",
        "glicko_rating", "rd", "volatility", "last_rated_on",  # the KAN-67 shadow
    )

    def __init__(self, player_id, club_id, season_id, format, rating):
        self.player_id, self.club_id = player_id, club_id
        self.season_id, self.format = season_id, format
        self.rating = self.peak = rating
        self.matches = 0
        self.glicko_rating = self.rd = self.volatility = self.last_rated_on = None

    @property
    def key(self):
        return (self.player_id, self.season_id, self.format)


class RatingBook:
    """Rows keyed by (player_id, season_id, format). Subclasses supply `new_row`."""

    def __init__(self):
        self.rows = {}

    def row(self, player_id, season_id, fmt):
        return self.rows.get((player_id, season_id, fmt))

    def new_row(self, player, scope, initial_rating):  # pragma: no cover - abstract
        raise NotImplementedError


def scope_reading(book, player_id, scope):
    """(rating, experience) for a player in a format scope, before this match.

    Experience is always the all-time count in the format (decision: a new
    season does not make an established player provisional again). A season
    rating not yet created is the carry-over seed from the all-time rating.
    """
    all_time = book.row(player_id, None, scope.format)
    experience = all_time.matches if all_time else 0
    if scope.season_id is None:
        return (all_time.rating if all_time else seasons.RATING_MEAN), experience
    row = book.row(player_id, scope.season_id, scope.format)
    if row is not None:
        return row.rating, experience
    return seasons.carry_over_seed(all_time.rating if all_time else None), experience


class FormatChange(NamedTuple):
    """One player's movement in one format scope in one match."""

    row: object
    player_id: int
    old: int
    new: int
    change: int
    k: float
    glicko: object = None  # the GlickoUpdate applied alongside, if any


def format_scopes(match, season):
    """The format scopes a match rates: all-time, plus its season if it has one."""
    fmt = match_format(match)
    scopes = [RatingScope(None, fmt)]
    if season is not None:
        scopes.append(RatingScope(season, fmt))
    return scopes


def _plan_glicko(match, scope, book, params):
    """The Glicko shadow projection for one scope, or None to skip it.

    The only guarded step of the pipeline: a failure here is logged and costs
    the shadow for this scope, never the Elo write.
    """
    try:
        projection = glicko_adapter.projected_glicko_changes(
            match, scope, book=book, params=params
        )
        if projection is not None and not projection.is_finite():
            raise ValueError(f"non-finite Glicko result: {projection!r}")
        return projection
    except Exception:  # Glicko2Error is the expected case; any other is handled the same
        logger.exception(
            f"Glicko-2 shadow skipped for match {getattr(match, 'pk', None)} in {scope}"
        )
        return None


def apply_format_scopes(match, book, season, *, base_k=None, glicko=None):
    """Project and apply every format scope of `match` to `book`, in memory.

    The shared step of the live write and the replay. All projections and all
    starting ratings are read **before** any row changes, so a season seed sees
    the all-time rating as it was before this match.

    `glicko` is a `GlickoParams` (None = no shadow). The shadow rides the same
    rows: its projections are planned before any mutation and applied after
    the Elo arithmetic, which it never touches.
    """
    team1_players, team2_players = match.sides()
    plan = []
    glicko_plan = {}
    for scope in format_scopes(match, season):
        if glicko is not None:
            projection = _plan_glicko(match, scope, book, glicko)
            if projection is not None:
                glicko_plan[scope] = projection
        change1, change2, k1, k2 = projected_elo_changes(match, scope, book=book, base_k=base_k)
        for players, delta, k in ((team1_players, change1, k1), (team2_players, change2, k2)):
            for p in players:
                old, _ = scope_reading(book, p.pk, scope)
                plan.append((scope, p, old, delta, k))

    changes = []
    for scope, p, old, delta, k in plan:
        row = book.row(p.pk, scope.season_id, scope.format)
        if row is None:
            row = book.new_row(p, scope, old)
        row.rating = old + delta
        row.peak = max(row.peak, row.rating)
        row.matches += 1
        changes.append(FormatChange(row, p.pk, old, row.rating, delta, k))

    if glicko_plan:
        match_day = match.date_played
        match_day = match_day.date() if hasattr(match_day, "hour") else match_day
        by_row = {}
        for scope, projection in glicko_plan.items():
            for update in projection.updates:
                row = book.row(update.player_id, scope.season_id, scope.format)
                glicko_adapter.apply_glicko(row, update, match_day)
                by_row[id(row)] = update
        changes = [c._replace(glicko=by_row.get(id(c.row))) for c in changes]
    return changes


class LiveBook(RatingBook):
    """PlayerRating rows for one match's players, loaded in one query.

    `lock=True` (inside `update_player_elo`'s transaction) takes the rows
    `select_for_update`, so two different matches for the same player
    serialise on PostgreSQL rather than losing an update.
    """

    def __init__(self, match, season=None, *, lock=False):
        from .models import PlayerRating

        super().__init__()
        self.season = season
        self.created = []
        side1, side2 = match.sides()
        ids = [p.pk for p in side1 + side2]
        season_q = Q(season__isnull=True)
        if season is not None:
            season_q |= Q(season=season)
        # all_objects: filtered by this match's own players, so transitively
        # club-scoped (KAN-24 audit), and a write must never be hidden by the
        # viewer's active club.
        qs = PlayerRating.all_objects.filter(
            season_q, player_id__in=ids, format=match_format(match)
        )
        if lock:
            qs = qs.select_for_update()
        for r in qs:
            self.rows[(r.player_id, r.season_id, r.format)] = r

    def new_row(self, player, scope, initial_rating):
        from .models import PlayerRating

        row = PlayerRating(
            player_id=player.pk,
            club_id=scope.season.club_id if scope.season is not None else player.club_id,
            season=scope.season,
            format=scope.format,
            rating=initial_rating,
            peak=initial_rating,
            matches=0,
        )
        self.rows[(player.pk, scope.season_id, scope.format)] = row
        self.created.append(row)
        return row

    def flush(self, match, changes):
        """Persist rows and RatingChange audit rows: three queries at most."""
        from .models import PlayerRating, RatingChange

        seen = {id(r) for r in self.created}
        if self.created:
            PlayerRating.all_objects.bulk_create(self.created)
        now = timezone.now()
        existing = []
        for change in changes:
            if id(change.row) not in seen:
                seen.add(id(change.row))
                change.row.updated_at = now
                existing.append(change.row)
        if existing:
            PlayerRating.all_objects.bulk_update(
                existing, ["rating", "peak", "matches", "updated_at", *glicko_adapter.SHADOW_FIELDS]
            )
        RatingChange.objects.bulk_create([
            RatingChange(
                match_id=match.pk,
                player_id=c.player_id,
                rating=c.row,
                old_rating=c.old,
                new_rating=c.new,
                rating_change=c.change,
                k_factor=c.k,
            )
            for c in changes
        ])


def projected_elo_change_for(match, player, scope=LEGACY):
    """What this match is worth to one player in `scope`, signed. 0 if they did not play."""
    if player is None:
        return 0
    projection = projected_elo_changes(match, scope)
    side1, side2 = match.sides()
    if any(p.pk == player.pk for p in side1):
        return projection.side1_change
    if any(p.pk == player.pk for p in side2):
        return projection.side2_change
    return 0


def _already_applied(match):
    """Has Elo been written for this match? The idempotency guard."""
    from .models import EloHistory

    return EloHistory.objects.filter(match=match).exists()


def update_player_elo(match):
    """
    Calculate and update Elo ratings after match completion.
    Called from handle_match_completion signal or management command.

    Handles both 1v1 and 2v2 matches.
    For 2v2, uses the average Elo of the team to calculate probabilities,
    then applies the same rating change to both players on the team.

    Writes, in one transaction: the legacy scope (`Player` + `EloHistory`,
    unchanged), the all-time format scope and the match's season scope
    (`PlayerRating` + `RatingChange`).

    **Race safety.** Two confirmers can both pass the guard. The unique
    constraints on `EloHistory (match, player)` and `RatingChange (match,
    rating)` then roll the loser's whole block back, and the loser treats that
    as "already applied". A conflict that is *not* this match being applied
    (two matches creating the same new rating row) is retried once.

    **Every attempt starts from locked, freshly read Player rows**
    (`_lock_sides`), never from the `match.sides()` memo. The memo can be stale
    (another match for the same player committed since it was loaded), and a
    failed attempt has already added its deltas to those objects in memory;
    the legacy fields are written back as absolute values, so either would be
    a silently wrong rating.
    """
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
    if _already_applied(match):
        logger.debug(f"Skipping Elo update for match {match.pk}: already calculated")
        return

    for attempt in (1, 2):
        try:
            _apply_match(match)
            return
        except IntegrityError:
            # The rollback undid the database, not the Player objects the
            # attempt mutated in memory. Drop them so nothing downstream (or
            # the retry) reads a rating that was never committed.
            match.invalidate_participant_cache()
            if _already_applied(match):
                logger.info(
                    f"Elo for match {match.pk} was applied by a concurrent confirmer"
                )
                return
            if attempt == 2:
                raise
            logger.warning(f"Rating write conflict on match {match.pk}; retrying once")


def _lock_sides(match):
    """Reload the match's players with `SELECT ... FOR UPDATE`; reset the memo.

    Must run inside the transaction. Rows are locked in player-pk order so two
    confirmations that share players cannot deadlock; the sides are then
    rebuilt in participant order, which is the order `sides()` and the replay
    use, so the arithmetic is unchanged. The new lists become the
    `match.sides()` memo, so `projected_elo_changes` -- the same call the
    preview makes -- reads exactly these rows for every scope.

    Same select_related as `Match.sides()`, so the rest of the completion
    path pays no extra queries; `of=("player",)` locks only the Player rows
    (PostgreSQL refuses FOR UPDATE on the nullable side of the user joins).
    """
    from .models import MatchParticipant, Side

    participants = list(
        MatchParticipant.objects.filter(match_id=match.pk)
        .select_related("player", "player__club", "player__user", "player__user__profile")
        .select_for_update(of=("player",))
        .order_by("player_id")
    )
    participants.sort(key=lambda mp: mp.pk)
    side1 = [mp.player for mp in participants if mp.side == Side.ONE]
    side2 = [mp.player for mp in participants if mp.side != Side.ONE]
    match.invalidate_participant_cache()
    match._sides_memo = (side1, side2)
    return side1, side2


def _apply_match(match):
    """Every scope for one match, atomically. Raises IntegrityError on a race."""
    from .models import EloHistory, Season

    with transaction.atomic():
        # 1. IDENTIFY SIDES AND PLAYERS -- re-read and locked, see _lock_sides.
        team1_players, team2_players = _lock_sides(match)

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

        # The format scopes, computed in memory before anything is written.
        season = Season.for_day(match.club_id, match.date_played)
        book = LiveBook(match, season, lock=True)
        format_changes = apply_format_scopes(
            match, book, season, glicko=glicko_adapter.DEFAULT_PARAMS
        )

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

        # 7. THE PlayerRating MIRROR
        book.flush(match, format_changes)

    # Logging
    team1_names = ", ".join([p.name for p in team1_players])
    team2_names = ", ".join([p.name for p in team2_players])

    logger.info(
        f"Elo updated for match {match.pk} ({'2v2' if is_double else '1v1'}): "
        f"Team1[{team1_names}] {elo_change_1:+d}, "
        f"Team2[{team2_names}] {elo_change_2:+d}"
    )

"""Season rollover (KAN-51, S4.6): freeze, archive, start the next, tell everyone.

Run nightly by the ``season_rollover`` job (``scheduled_jobs.py``). The
contract is "Rollover (S4.6)" in ``docs/decisions/kan-9-seasons.md``.

* **Step 1, the successor, ahead of time.** Once a club's latest season (by
  ``ends_on``) is within ``SUCCESSOR_LEAD_DAYS`` of its end, the next
  contiguous season is created, unless the club already has a season starting
  after it ends. It must exist before its first day: a match confirmed on a
  day in no season gets all-time ratings only and is never back-filled.
  One transaction per creation, under a lock on the latest season row.
* **Step 2, the archive.** A season is **due** once
  ``ends_on + ROLLOVER_GRACE_DAYS < today`` and it is not archived. The grace
  lets a match played on the last day be confirmed into the table. Per
  season, one transaction: freeze the final table into ``SeasonStanding``
  rows (from the season's ``PlayerRating`` rows, wins and losses from its
  ``RatingChange`` rows) and set ``archived_at``. The season row is locked
  and re-checked inside the transaction, so a concurrent run archives it once.
* **Notifications come after the commit**, never inside it (a rolled-back
  freeze must not have told anyone anything). Each player is claimed with a
  conditional ``.update()`` of ``notified_at`` across their rows in the
  season: whoever flips it from NULL sends, so a re-run, a concurrent run or a
  crash between archive and notify never tells anyone twice. A crash *after*
  the claim and before the send loses that one message (at most once).
* Jobs have no viewer, so every query here is unfiltered: each read is
  explicitly bounded by a season, and a season belongs to one club.

The frozen rows are never touched again. A late confirmation into an archived
season still moves ``PlayerRating`` (decided in the contract); the table does
not move with it.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Exists, Max, OuterRef
from django.urls import reverse
from django.utils import timezone

from . import emails, flags, notifications, ratings, seasons
from .models import (
    MatchParticipant,
    Player,
    PlayerRating,
    RatingChange,
    RatingFormat,
    Season,
    SeasonStanding,
)

logger = logging.getLogger(__name__)

# Standings written per INSERT, and PlayerRating rows read per round trip.
STANDINGS_BATCH_SIZE = 500
# (season, player) pairs claimed per notification pass.
NOTIFY_BATCH_SIZE = 200

# Formats in the order a player reads them ("#1 in Singles, #3 in Doubles").
FORMAT_ORDER = [RatingFormat.SINGLES, RatingFormat.DOUBLES]


def due_seasons(today):
    """Unarchived seasons whose grace days have fully passed by `today`."""
    cutoff = today - timedelta(days=seasons.ROLLOVER_GRACE_DAYS)
    return Season.all_objects.filter(archived_at__isnull=True, ends_on__lt=cutoff)


def clubs_needing_successor(today):
    """Club ids whose latest season (by ends_on) is within its lead window."""
    cutoff = today + timedelta(days=seasons.SUCCESSOR_LEAD_DAYS)
    return (
        Season.all_objects.values("club_id").annotate(latest=Max("ends_on"))
        .filter(latest__lte=cutoff).order_by("club_id").values_list("club_id", flat=True)
    )


def _latest_season_pk(club_id):
    return (
        Season.all_objects.filter(club_id=club_id)
        .order_by("-ends_on", "-pk").values_list("pk", flat=True).first()
    )


def run(*, today=None, dry_run=False):
    """Create due successors, then roll over every due season. Returns a summary.

    A club or season that fails is rolled back, logged and skipped so the
    others still go through; the run then raises so the JobRun records the
    failure. A backlog (the job did not run for a while) catches up in one
    run: step 1 loops until every club's latest season is outside its lead
    window, and step 2 archives every season already past its grace days.
    """
    today = today or timezone.now().date()
    if dry_run:
        return _dry_run_summary(today)

    created, failed_clubs = create_successors(today)

    archived = frozen = 0
    failed = []
    while True:
        pk = (
            due_seasons(today).exclude(pk__in=failed)
            .order_by("ends_on", "pk").values_list("pk", flat=True).first()
        )
        if pk is None:
            break
        try:
            rows = archive_season(pk, today=today)
        except Exception:
            logger.exception("Season %s failed to roll over; rolled back", pk)
            failed.append(pk)
            continue
        if rows is None:
            continue
        archived += 1
        frozen += rows

    told = notify_pending()
    summary = (
        f"created {created} next season{'s' if created != 1 else ''}, "
        f"archived {archived} season{'s' if archived != 1 else ''} ({frozen} standings), "
        f"notified {told} players"
    )
    if failed or failed_clubs:
        raise RuntimeError(f"{summary}; failed seasons: {failed}; failed clubs: {failed_clubs}")
    return summary


def create_successors(today):
    """Step 1: give every club whose latest season ends soon its next season.

    Returns ``(created, failed_club_ids)``. Loops, so a backlog creates every
    missing season up to today's lead window; each pass creates a later
    season, so it terminates.
    """
    created = 0
    failed, settled = [], []
    while True:
        club_id = clubs_needing_successor(today).exclude(club_id__in=failed + settled).first()
        if club_id is None:
            return created, failed
        try:
            new = _create_successor(_latest_season_pk(club_id), today=today)
        except Exception:
            logger.exception("Club %s: next season failed to be created; rolled back", club_id)
            failed.append(club_id)
            continue
        if new is None:
            # Nothing to do after all (a concurrent run won); never loop on it.
            settled.append(club_id)
            continue
        created += 1


def _create_successor(season_pk, *, today):
    """Create the season after `season_pk`, atomically. None if not (or no longer) needed."""
    with transaction.atomic():
        season = Season.all_objects.select_for_update().get(pk=season_pk)
        if not seasons.needs_successor(season.ends_on, today):
            return None
        return _create_next_season(season)


def archive_season(season_pk, *, today):
    """Freeze and archive one season, atomically.

    Returns the number of standings written, or None when the season was
    already archived (or is not due) by the time the lock was held.
    """
    with transaction.atomic():
        season = Season.all_objects.select_for_update().get(pk=season_pk)
        if season.archived_at is not None or not seasons.is_due_for_rollover(season.ends_on, today):
            return None
        rows = _freeze(season)
        Season.all_objects.filter(pk=season.pk).update(archived_at=timezone.now())
    return rows


def _wins_and_played(season, fmt):
    """{PlayerRating id: (wins, played)} from the season's audit trail."""
    changes = RatingChange.objects.filter(rating__season=season, rating__format=fmt)
    played = dict(changes.values("rating_id").annotate(n=Count("pk")).values_list("rating_id", "n"))
    won = Exists(
        MatchParticipant.objects.filter(
            match_id=OuterRef("match_id"),
            player_id=OuterRef("player_id"),
            side=OuterRef("match__winner_side"),
        )
    )
    wins = dict(
        changes.filter(won).values("rating_id").annotate(n=Count("pk")).values_list("rating_id", "n")
    )
    return {rid: (wins.get(rid, 0), n) for rid, n in played.items()}


def _freeze(season):
    """Write the season's final table, every format. Returns rows written.

    The system is `ratings.system(None)` (the settings flags): under Glicko the
    table is ranked by `glicko_rating`, rows with no Glicko state are left out,
    and each standing is stamped with `rating_system` and its display `rd`
    (inflated to the freeze day). The stamp is what lets the archive keep
    labelling a past table correctly after a rollback.
    """
    system = ratings.system(None)
    glicko = system == ratings.GLICKO
    sort_field = "glicko_rating" if glicko else "rating"
    # Inflated to the season's last day, not the day the job ran: a cron
    # backlog must not change a frozen +/-.
    today = season.ends_on
    written = 0
    for fmt in FORMAT_ORDER:
        results = _wins_and_played(season, fmt)
        rows = PlayerRating.all_objects.filter(season=season, format=fmt)
        if glicko:
            rows = rows.filter(glicko_rating__isnull=False)
        rows = (
            rows.select_related("player").order_by(f"-{sort_field}", "pk")
            .iterator(chunk_size=STANDINGS_BATCH_SIZE)
        )
        batch = []
        for rank, row in seasons.ranked(rows, key=lambda r: getattr(r, sort_field)):
            wins, played = results.get(row.pk, (0, 0))
            shown = ratings.display_rd(row, today) if glicko else None
            batch.append(SeasonStanding(
                season=season, format=fmt, player=row.player,
                player_name=str(row.player)[:100], rank=rank,
                rating=round(row.glicko_rating) if glicko else row.rating,
                matches=row.matches, wins=wins, losses=played - wins,
                rating_system=system,
                rd=shown,
            ))
            if len(batch) >= STANDINGS_BATCH_SIZE:
                SeasonStanding.all_objects.bulk_create(batch)
                written += len(batch)
                batch = []
        if batch:
            SeasonStanding.all_objects.bulk_create(batch)
            written += len(batch)
    return written


def _create_next_season(season):
    """The season after this one, same length, unless the club already has a later one.

    Any season that would overlap the new one starts after `season` ends
    (seasons never overlap), so "nothing later exists" also means the new
    row cannot hit the database's non-overlap constraint.
    """
    if Season.all_objects.filter(club_id=season.club_id, starts_on__gt=season.ends_on).exists():
        return None
    starts_on, ends_on = seasons.next_season_bounds(season.starts_on, season.ends_on)
    return Season.all_objects.create(
        club_id=season.club_id,
        name=seasons.season_name_for(starts_on, ends_on),
        starts_on=starts_on,
        ends_on=ends_on,
    )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def notify_pending():
    """Tell every not-yet-claimed ranked player where they finished.

    Returns how many players were sent a push or an email. Claimed rows whose
    player cannot be told (deleted, no account, `seasons` flag off for them)
    stay claimed: turning the flag on later must not dig up an old season.
    """
    now = timezone.now()
    SeasonStanding.all_objects.filter(notified_at__isnull=True, player__isnull=True).update(notified_at=now)

    told = 0
    seasons_by_pk = {}
    while True:
        batch = list(
            SeasonStanding.all_objects.filter(notified_at__isnull=True)
            .order_by("season_id", "player_id")
            .values_list("season_id", "player_id").distinct()[:NOTIFY_BATCH_SIZE]
        )
        if not batch:
            break
        for season_id, player_id in batch:
            claimed = SeasonStanding.all_objects.filter(
                season_id=season_id, player_id=player_id, notified_at__isnull=True
            ).update(notified_at=now)
            if not claimed:
                continue  # a concurrent run got there first
            if season_id not in seasons_by_pk:
                seasons_by_pk[season_id] = Season.all_objects.get(pk=season_id)
            if _tell(seasons_by_pk[season_id], player_id):
                told += 1
    return told


def _tell(season, player_id):
    """Push, or email whoever push did not reach. True if anything was sent."""
    # _base_manager: the standing belongs to this season's club, and a player
    # who has since moved club must still be reached (transitively scoped).
    player = Player._base_manager.select_related("user__profile").get(pk=player_id)
    user = player.user
    if user is None or not flags.is_enabled("seasons", user):
        return False

    rows = dict(
        SeasonStanding.all_objects.filter(season=season, player_id=player_id).values_list("format", "rank")
    )
    standings = [(fmt.label, rows[fmt.value]) for fmt in FORMAT_ORDER if fmt.value in rows]
    message = {
        "season_name": season.name,
        "standings": standings,
        "url": reverse("pingpong:season_archive_detail", args=[season.pk]),
    }

    reached = notifications.notify_season_ended(player, **message)
    if player.pk in reached:
        return True
    profile = getattr(user, "profile", None)
    if user.email and profile is not None and profile.email_verified:
        emails.send_season_ended_email(player, **message)
        return True
    return False


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def _dry_run_summary(today):
    """What a real run would do now, writing nothing.

    Counts one successor per club whose latest season is in its lead window,
    and only seasons due today; a backlog's follow-on seasons (created and
    then archived in the same real run) are not simulated.
    """
    due = list(due_seasons(today).values_list("pk", flat=True))
    standings = PlayerRating.all_objects.filter(season_id__in=due).count()
    to_create = clubs_needing_successor(today).count()
    pending = (
        SeasonStanding.all_objects.filter(notified_at__isnull=True, player__isnull=False)
        .values("season_id", "player_id").distinct().count()
    )
    return (
        f"would create {to_create} next season{'s' if to_create != 1 else ''}, "
        f"archive {len(due)} season{'s' if len(due) != 1 else ''} ({standings} standings), "
        f"notify {pending} pending players (plus those ranked by this run)"
    )

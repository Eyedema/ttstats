"""Assign every pre-tenancy row to a club.

The highest-consequence data change in the epic. Once the managers start
filtering (S1.3), a row still holding a null club is invisible to everyone,
which users experience as data loss -- so this has to be complete, repeatable,
and reversible before the flag is ever flipped.

The logic lives here, outside both the data migration and the management
command, because they need to run *the same* rules against different model
classes: the migration gets historical models from `apps.get_model()`, the
command gets the real ones. Hence `Models`, a bundle passed in rather than
imported -- the same shape as `match_state.py` and `live_scoring.py` keeping
their decisions out of Django's way.

Rules
-----
* A **match's** club comes from its *participants*, not from `created_by`.
  A match is routinely entered by one player on behalf of others, so the
  creator is not evidence of whose data it is.
* A **championship's** club comes from its entry members, then its creator,
  then the default.
* **Players**, **locations** and anything that resolves to nothing get the
  default club.
* Every existing **User** gets a membership of the default club. Without this
  the backfill is technically complete and the app is still empty for
  everyone: S1.4 resolves an active club from memberships, and a user with no
  membership has no active club.

Nothing here creates or deletes an application row. It only assigns -- and the
membership rows, which are new by necessity. Row counts for Player, Match,
ScheduledMatch, Championship, Location and Game are identical before and after.
"""
import logging
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500

# The one club every pre-tenancy row lands in. Looked up by slug so the
# migration and the repair command cannot create two of them.
DEFAULT_CLUB_SLUG = "ttstats"
DEFAULT_CLUB_NAME = "TTStats"


@dataclass
class Models:
    """The model classes to operate on.

    Passed in rather than imported so the data migration can hand over
    historical models, which is the only way a migration stays correct when the
    real models move on.
    """

    Club: type
    ClubMembership: type
    User: type
    Player: type
    Location: type
    Match: type
    ScheduledMatch: type
    Championship: type
    MatchParticipant: type
    ScheduledMatchParticipant: type
    ChampionshipEntryMember: type


@dataclass
class EntityReport:
    examined: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass
class Report:
    """What a run examined, changed and could not make sense of."""

    club_created: bool = False
    entities: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)

    def for_entity(self, name):
        return self.entities.setdefault(name, EntityReport())

    @property
    def total_updated(self):
        return sum(e.updated for e in self.entities.values())

    def lines(self):
        yield (
            f"Default club: {'created' if self.club_created else 'already existed'}"
        )
        for name, entry in self.entities.items():
            yield (
                f"  {name:<16} examined {entry.examined:>6}  "
                f"updated {entry.updated:>6}  skipped {entry.skipped:>6}"
            )
        yield f"  {'TOTAL UPDATED':<16} {self.total_updated}"
        if self.anomalies:
            yield f"Anomalies ({len(self.anomalies)}):"
            for line in self.anomalies:
                yield f"  ! {line}"
        else:
            yield "Anomalies: none"


def get_or_create_default_club(Club):
    """The default club, by slug. Returns (club, created)."""
    club = Club.objects.filter(slug=DEFAULT_CLUB_SLUG).first()
    if club is not None:
        return club, False
    return (
        Club.objects.create(slug=DEFAULT_CLUB_SLUG, name=DEFAULT_CLUB_NAME),
        True,
    )


def _batched(queryset, batch_size):
    """Yield lists of pks. `.iterator()` so a large table is not materialised.

    The VPS has 3.7GB and the backfill has to run while the app is serving, so
    holding every match in memory at once is not acceptable even at today's
    row counts.
    """
    batch = []
    for pk in queryset.values_list("pk", flat=True).iterator(chunk_size=batch_size):
        batch.append(pk)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _resolve_from_votes(votes, default_club_id, describe, report):
    """Pick one club id from the clubs of a row's members.

    Zero clubs (no members, or members with no club yet) -> the default.
    One -> that one. More than one -> the most common, ties broken by lowest
    id so a re-run is deterministic, and the disagreement is logged rather
    than raised. A cross-club row is a data problem worth a human's attention;
    it is not a reason to abort a backfill halfway through.
    """
    clubs = [c for c in votes if c is not None]
    if not clubs:
        return default_club_id
    distinct = set(clubs)
    if len(distinct) == 1:
        return distinct.pop()

    counts = Counter(clubs)
    winner = min(counts, key=lambda cid: (-counts[cid], cid))
    message = (
        f"{describe} spans clubs {sorted(distinct)}; assigned {winner}"
    )
    logger.warning(message)
    report.anomalies.append(message)
    return winner


def _assign_flat(model, default_club_id, name, report, batch_size, dry_run):
    """Everything with no way to derive a club: players, locations."""
    unassigned = model.objects.filter(club_id__isnull=True)
    entry = report.for_entity(name)
    for pks in _batched(unassigned, batch_size):
        entry.examined += len(pks)
        if dry_run:
            entry.updated += len(pks)
            continue
        entry.updated += model.objects.filter(
            pk__in=pks, club_id__isnull=True
        ).update(club_id=default_club_id)


def _assign_by_members(
    model,
    through,
    parent_field,
    default_club_id,
    name,
    report,
    batch_size,
    dry_run,
):
    """Matches and scheduled matches: club derived from participants."""
    unassigned = model.objects.filter(club_id__isnull=True)
    entry = report.for_entity(name)

    for pks in _batched(unassigned, batch_size):
        entry.examined += len(pks)

        votes = {}
        rows = through.objects.filter(
            **{f"{parent_field}_id__in": pks}
        ).values_list(f"{parent_field}_id", "player__club_id")
        for parent_id, club_id in rows:
            votes.setdefault(parent_id, []).append(club_id)

        # Group by resolved club so each batch is a handful of UPDATEs rather
        # than one per row.
        by_club = {}
        for pk in pks:
            members = votes.get(pk)
            if not members:
                # A match with zero participants. Real: MatchParticipant.player
                # is CASCADE, so deleting a player empties a side -- a
                # documented production incident. It still needs a club, or the
                # scoping filters make it permanently invisible.
                message = f"{name} {pk} has no participants; assigned the default club"
                logger.warning(message)
                report.anomalies.append(message)
                club_id = default_club_id
            else:
                club_id = _resolve_from_votes(
                    members, default_club_id, f"{name} {pk}", report
                )
            by_club.setdefault(club_id, []).append(pk)

        if dry_run:
            entry.updated += len(pks)
            continue
        for club_id, group in by_club.items():
            entry.updated += model.objects.filter(
                pk__in=group, club_id__isnull=True
            ).update(club_id=club_id)


def _assign_championships(
    models, default_club_id, report, batch_size, dry_run
):
    """Championships: entry members, then the creator, then the default."""
    Championship = models.Championship
    unassigned = Championship.objects.filter(club_id__isnull=True)
    entry = report.for_entity("championship")

    for pks in _batched(unassigned, batch_size):
        entry.examined += len(pks)

        votes = {}
        rows = models.ChampionshipEntryMember.objects.filter(
            championship_id__in=pks
        ).values_list("championship_id", "player__club_id")
        for champ_id, club_id in rows:
            votes.setdefault(champ_id, []).append(club_id)

        creators = dict(
            Championship.objects.filter(pk__in=pks).values_list(
                "pk", "created_by__club_id"
            )
        )

        by_club = {}
        for pk in pks:
            members = votes.get(pk) or []
            if not members and creators.get(pk):
                members = [creators[pk]]
            club_id = _resolve_from_votes(
                members, default_club_id, f"championship {pk}", report
            )
            by_club.setdefault(club_id, []).append(pk)

        if dry_run:
            entry.updated += len(pks)
            continue
        for club_id, group in by_club.items():
            entry.updated += Championship.objects.filter(
                pk__in=group, club_id__isnull=True
            ).update(club_id=club_id)


def _assign_memberships(models, default_club_id, report, batch_size, dry_run):
    """Give every existing user a membership of the club their player is in.

    Without this the backfill assigns every row correctly and the app is still
    empty for everyone: S1.4 resolves the active club from memberships, and a
    user with no membership has no active club at all.
    """
    ClubMembership = models.ClubMembership
    entry = report.for_entity("membership")

    already = set(
        ClubMembership.objects.values_list("user_id", "club_id")
    )
    player_club = dict(
        models.Player.objects.exclude(user_id=None).values_list(
            "user_id", "club_id"
        )
    )

    to_create = []
    for pks in _batched(models.User.objects.all(), batch_size):
        entry.examined += len(pks)
        for user_id in pks:
            club_id = player_club.get(user_id) or default_club_id
            if (user_id, club_id) in already:
                entry.skipped += 1
                continue
            already.add((user_id, club_id))
            to_create.append(
                ClubMembership(user_id=user_id, club_id=club_id)
            )
            entry.updated += 1

    if to_create and not dry_run:
        ClubMembership.objects.bulk_create(to_create, batch_size=batch_size)


def run(models, batch_size=DEFAULT_BATCH_SIZE, dry_run=False):
    """Assign every unassigned row to a club. Idempotent.

    Every write is filtered on `club_id__isnull=True`, so a second run reports
    zero updates and changes nothing -- and two runs racing each other cannot
    reassign a row the other just claimed.
    """
    report = Report()

    club, created = (
        get_or_create_default_club(models.Club)
        if not dry_run
        else _peek_default_club(models.Club)
    )
    report.club_created = created
    default_club_id = club.pk if club is not None else None

    # Players first: everything else derives its club from them.
    _assign_flat(models.Player, default_club_id, "player", report, batch_size, dry_run)
    _assign_flat(
        models.Location, default_club_id, "location", report, batch_size, dry_run
    )
    _assign_by_members(
        models.Match,
        models.MatchParticipant,
        "match",
        default_club_id,
        "match",
        report,
        batch_size,
        dry_run,
    )
    _assign_by_members(
        models.ScheduledMatch,
        models.ScheduledMatchParticipant,
        "scheduled_match",
        default_club_id,
        "scheduled_match",
        report,
        batch_size,
        dry_run,
    )
    _assign_championships(models, default_club_id, report, batch_size, dry_run)
    _assign_memberships(models, default_club_id, report, batch_size, dry_run)

    return report


def _peek_default_club(Club):
    """The default club without creating it -- for --dry-run.

    Returns (None, True) when it does not exist yet, so a dry run on a virgin
    database reports "would create" rather than creating one as a side effect
    of being asked what it would do.
    """
    club = Club.objects.filter(slug=DEFAULT_CLUB_SLUG).first()
    return club, club is None


def reverse(models):
    """Undo the assignment: null every club FK, drop memberships and the club.

    Order matters -- every club FK is PROTECT, so the club cannot be deleted
    until nothing points at it. `0028_drop_team` made itself irreversible and
    the documented remedy became "restore a snapshot"; the rollback plan in
    T1.12 depends on this one not doing that.
    """
    for model in (
        models.Match,
        models.ScheduledMatch,
        models.Championship,
        models.Player,
        models.Location,
    ):
        model.objects.filter(club_id__isnull=False).update(club_id=None)

    models.ClubMembership.objects.all().delete()
    models.Club.objects.filter(slug=DEFAULT_CLUB_SLUG).delete()

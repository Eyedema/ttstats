"""Write-side helpers shared by the match/scheduled-match views.

Team assembly used to be inlined three times (MatchCreateView,
ScheduledMatchCreateView, ScheduledMatchConvertView) with subtly different
rules -- notably the doubles branch of MatchCreateView omitted the team-size
guard, so it could reuse an over-sized team that merely contained both
players. One implementation, one rule.
"""
from django.db.models import Count

from .models import (
    MatchParticipant,
    ScheduledMatchParticipant,
    Side,
    Team,
)


def resolve_team(players):
    """Return the Team whose membership is exactly ``players``, creating it if
    it does not exist yet.

    Teams are reused rather than created per match: in practice each player
    has one canonical singleton team, and each doubles pairing one team.
    """
    players = list(players)
    if not players:
        raise ValueError("A team needs at least one player")

    # The size annotation must come BEFORE the membership filters. Annotating
    # after a filter on the same m2m makes Django reuse the filtered join, so
    # the count only ever sees the matched rows and equals 1 -- which is why
    # the old inline version could hand back a doubles team for a singles
    # lookup. Filtering after the annotation creates fresh joins instead.
    candidates = (
        Team.objects
        .annotate(num_players=Count("players", distinct=True))
        .filter(num_players=len(players))
    )
    for player in players:
        candidates = candidates.filter(players=player)

    existing = candidates.first()
    if existing is not None:
        return existing

    team = Team.objects.create()
    team.players.set(players)
    return team


def resolve_sides(side1_players, side2_players):
    """Return ``(team1, team2)`` for the two sides of a match."""
    return resolve_team(side1_players), resolve_team(side2_players)


def _sync_participants(parent, participant_model, parent_field):
    """Reconcile participant rows against the parent's team membership.

    Idempotent, so it is safe to call on every save. This is the transitional
    bridge: Team stays the source of truth until the read paths have moved.
    """
    if not parent.pk:
        return

    desired = {}
    for side, team in (
        (Side.ONE, parent.team1),
        (Side.TWO, parent.team2),
    ):
        if team is None:
            continue
        for player_id in team.players.values_list("id", flat=True):
            # A player somehow on both sides keeps the lower side, matching
            # the backfill migration.
            desired.setdefault(player_id, side)

    existing = {p.player_id: p for p in parent.participants.all()}

    stale = [p.pk for player_id, p in existing.items() if player_id not in desired]
    if stale:
        participant_model.objects.filter(pk__in=stale).delete()

    to_create = []
    for player_id, side in desired.items():
        current = existing.get(player_id)
        if current is None:
            to_create.append(
                participant_model(
                    **{parent_field: parent}, player_id=player_id, side=side
                )
            )
        elif current.side != side:
            current.side = side
            current.save(update_fields=["side"])

    if to_create:
        participant_model.objects.bulk_create(to_create, ignore_conflicts=True)


def sync_match_participants(match):
    _sync_participants(match, MatchParticipant, "match")


def sync_scheduled_match_participants(scheduled_match):
    _sync_participants(
        scheduled_match, ScheduledMatchParticipant, "scheduled_match"
    )

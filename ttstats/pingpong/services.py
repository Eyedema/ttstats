"""Write-side helpers shared by the match/scheduled-match views.

Team assembly used to be inlined three times (MatchCreateView,
ScheduledMatchCreateView, ScheduledMatchConvertView) with subtly different
rules -- notably the doubles branch of MatchCreateView omitted the team-size
guard, so it could reuse an over-sized team that merely contained both
players. One implementation, one rule.
"""
from django.db.models import Count

from .models import Team


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

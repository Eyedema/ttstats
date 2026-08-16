"""Write-side helpers shared by the match/scheduled-match views."""
from .models import (
    ChampionshipEntry,
    MatchParticipant,
    ScheduledMatchParticipant,
    Side,
)


def set_match_sides(match, side1_players, side2_players):
    """Replace a match's participants with the given players."""
    _set_sides(match, MatchParticipant, "match", side1_players, side2_players)


def set_scheduled_match_sides(scheduled_match, side1_players, side2_players):
    _set_sides(
        scheduled_match,
        ScheduledMatchParticipant,
        "scheduled_match",
        side1_players,
        side2_players,
    )


def _set_sides(parent, participant_model, parent_field, side1_players, side2_players):
    participant_model.objects.filter(**{parent_field: parent}).delete()
    participant_model.objects.bulk_create([
        participant_model(**{parent_field: parent}, player=player, side=side)
        for side, players in (
            (Side.ONE, side1_players),
            (Side.TWO, side2_players),
        )
        for player in players
    ])


def link_championship_entries(obj):
    """Point side1_entry / side2_entry at the entries matching each side.

    Runs on save for anything in a championship whose entry links are not set
    yet, so converting a scheduled match, using the admin or building one in a
    factory all end up linked without each remembering to. Already-set links
    are left alone.
    """
    if not obj.championship_id:
        return
    if obj.side1_entry_id and obj.side2_entry_id:
        return

    entries = ChampionshipEntry.objects.filter(
        championship_id=obj.championship_id
    ).prefetch_related("members")
    by_members = {
        frozenset(m.player_id for m in entry.members.all()): entry
        for entry in entries
    }

    updates = {}
    for attr, side in (("side1_entry", Side.ONE), ("side2_entry", Side.TWO)):
        if getattr(obj, f"{attr}_id"):
            continue
        key = frozenset(
            p.player_id for p in obj.participants.all() if p.side == side
        )
        entry = by_members.get(key)
        if entry is not None:
            updates[attr] = entry

    if not updates:
        return

    type(obj).all_objects.filter(pk=obj.pk).update(
        **{f"{attr}_id": entry.pk for attr, entry in updates.items()}
    )
    for attr, entry in updates.items():
        setattr(obj, attr, entry)

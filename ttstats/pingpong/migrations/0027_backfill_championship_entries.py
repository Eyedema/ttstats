"""Backfill ChampionshipEntry / ChampionshipEntryMember from participant Teams.

One entry per registered Team, its members taken from that team's players.
Existing championship matches and scheduled matches are then linked to the
entries by matching each side's player set.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

BATCH = 500


def _entry_key(player_ids):
    return frozenset(player_ids)


def backfill(apps, schema_editor):
    Championship = apps.get_model("pingpong", "Championship")
    ChampionshipEntry = apps.get_model("pingpong", "ChampionshipEntry")
    ChampionshipEntryMember = apps.get_model("pingpong", "ChampionshipEntryMember")
    Match = apps.get_model("pingpong", "Match")
    ScheduledMatch = apps.get_model("pingpong", "ScheduledMatch")

    entries_by_championship = {}
    entry_count = 0
    skipped_duplicate_players = 0

    for championship in Championship.objects.prefetch_related(
        "participants__players"
    ):
        seen_players = set()
        key_to_entry = {}

        for team in championship.participants.all():
            player_ids = [p.id for p in team.players.all()]

            # The old M2M allowed the same player to register twice (via two
            # different teams); the new schema forbids it. Keep the first.
            clashing = [pid for pid in player_ids if pid in seen_players]
            if clashing:
                skipped_duplicate_players += len(clashing)
                logger.warning(
                    "Championship %s: player(s) %s already entered; "
                    "skipping duplicate team %s",
                    championship.id,
                    clashing,
                    team.id,
                )
                continue

            entry = ChampionshipEntry.objects.create(
                championship=championship,
                display_name=team.name or "",
            )
            entry_count += 1
            ChampionshipEntryMember.objects.bulk_create(
                [
                    ChampionshipEntryMember(
                        entry=entry, player_id=pid, championship=championship
                    )
                    for pid in player_ids
                ],
                batch_size=BATCH,
            )
            seen_players.update(player_ids)
            key_to_entry[_entry_key(player_ids)] = entry

        entries_by_championship[championship.id] = key_to_entry

    linked, unmatched = _link_sides(
        Match.objects.filter(championship__isnull=False), entries_by_championship, Match
    )
    s_linked, s_unmatched = _link_sides(
        ScheduledMatch.objects.filter(championship__isnull=False),
        entries_by_championship,
        ScheduledMatch,
    )

    summary = (
        f"Created {entry_count} championship entries "
        f"({skipped_duplicate_players} duplicate player registrations skipped). "
        f"Linked {linked} matches and {s_linked} scheduled matches to entries; "
        f"{unmatched} matches and {s_unmatched} scheduled matches had a side "
        f"matching no entry and were left NULL."
    )
    logger.info(summary)
    print(summary)


def _link_sides(queryset, entries_by_championship, model):
    """Point side1_entry / side2_entry at the entry with the same player set."""
    to_update = []
    unmatched = 0

    for obj in queryset.select_related("team1", "team2").prefetch_related(
        "team1__players", "team2__players"
    ):
        key_to_entry = entries_by_championship.get(obj.championship_id, {})
        missing = False

        for attr, team in (("side1_entry", obj.team1), ("side2_entry", obj.team2)):
            entry = None
            if team is not None:
                entry = key_to_entry.get(_entry_key(p.id for p in team.players.all()))
            if entry is None:
                missing = True
            setattr(obj, attr, entry)

        if missing:
            unmatched += 1
        to_update.append(obj)

    model.objects.bulk_update(
        to_update, ["side1_entry", "side2_entry"], batch_size=BATCH
    )
    return len(to_update) - unmatched, unmatched


def unbackfill(apps, schema_editor):
    apps.get_model("pingpong", "Match").objects.all().update(
        side1_entry=None, side2_entry=None
    )
    apps.get_model("pingpong", "ScheduledMatch").objects.all().update(
        side1_entry=None, side2_entry=None
    )
    apps.get_model("pingpong", "ChampionshipEntryMember").objects.all().delete()
    apps.get_model("pingpong", "ChampionshipEntry").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pingpong", "0026_championship_entries"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]

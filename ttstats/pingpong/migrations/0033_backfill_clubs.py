"""Assign every existing row to a default club.

The highest-consequence data change in the tenancy epic: once S1.3's managers
start filtering, a row with a null club is invisible to everyone, which users
experience as data loss.

The rules live in `pingpong/club_backfill.py` so this migration and the
`backfill_clubs` repair command cannot drift apart. They are handed *historical*
models here -- the only way a migration stays correct as the real models move on.

Unlike `0028_drop_team`, this migration has a real reverse. The rollback plan in
T1.12 depends on it, and "restore a snapshot" is not an acceptable remedy for a
step that is meant to be routine.

Row counts are identical before and after for every application table. This
assigns; it never creates or deletes -- with the deliberate exception of
ClubMembership rows, which are new by necessity: without them every existing
user would resolve to no active club and find the app empty.
"""
import logging

from django.db import migrations

from pingpong import club_backfill

logger = logging.getLogger(__name__)


def _models(apps):
    return club_backfill.Models(
        Club=apps.get_model("pingpong", "Club"),
        ClubMembership=apps.get_model("pingpong", "ClubMembership"),
        User=apps.get_model("auth", "User"),
        Player=apps.get_model("pingpong", "Player"),
        Location=apps.get_model("pingpong", "Location"),
        Match=apps.get_model("pingpong", "Match"),
        ScheduledMatch=apps.get_model("pingpong", "ScheduledMatch"),
        Championship=apps.get_model("pingpong", "Championship"),
        MatchParticipant=apps.get_model("pingpong", "MatchParticipant"),
        ScheduledMatchParticipant=apps.get_model(
            "pingpong", "ScheduledMatchParticipant"
        ),
        ChampionshipEntryMember=apps.get_model(
            "pingpong", "ChampionshipEntryMember"
        ),
    )


def backfill(apps, schema_editor):
    report = club_backfill.run(_models(apps))
    for line in report.lines():
        logger.info(line)
        print(line)


def unbackfill(apps, schema_editor):
    club_backfill.reverse(_models(apps))
    logger.info("Cleared all club assignments and removed the default club.")


class Migration(migrations.Migration):

    dependencies = [
        ("pingpong", "0032_club_models"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]

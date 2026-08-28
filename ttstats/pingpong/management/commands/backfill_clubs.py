"""Assign any row still holding a null club to a club.

The data migration (`0033_backfill_clubs`) handles rows that exist at migration
time. This command handles the rest: rows created between that migration and the
flag flip, and any row a later bug leaves unassigned. It is the repair step in
the T1.12 runbook.

Deliberately re-runnable. Every write is filtered on `club_id__isnull=True`, so
a second run reports zero updates and changes nothing.

    python manage.py backfill_clubs --dry-run
    python manage.py backfill_clubs
    python manage.py backfill_clubs --batch-size 200

Safe to run while the app is serving traffic: it takes no long-lived lock, works
in batches, and only ever fills in a null.

**It must not run inside the deploy step.** `ssh-action`'s default timeout is ten
minutes and has caused deploy timeouts before; this is a separate, manually
invoked command.
"""
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from pingpong import club_backfill
from pingpong.models import (
    Championship,
    ChampionshipEntryMember,
    Club,
    ClubMembership,
    Location,
    Match,
    MatchParticipant,
    Player,
    ScheduledMatch,
    ScheduledMatchParticipant,
)


def command_models():
    """The real models, reaching past row-level security.

    `all_objects` throughout for anything that has it: a management command has
    no user in thread-local context, so `Match.objects` would be filtered by a
    user that does not exist. `Player`, `Location` and the participant tables
    have no row-level manager, so their default manager is already unfiltered.
    """
    return club_backfill.Models(
        Club=Club,
        ClubMembership=ClubMembership,
        User=User,
        Player=Player,
        Location=Location,
        Match=_Unfiltered(Match),
        ScheduledMatch=_Unfiltered(ScheduledMatch),
        Championship=_Unfiltered(Championship),
        MatchParticipant=MatchParticipant,
        ScheduledMatchParticipant=ScheduledMatchParticipant,
        ChampionshipEntryMember=ChampionshipEntryMember,
    )


class _Unfiltered:
    """A model proxy whose `.objects` is `all_objects`.

    `club_backfill` is written against `Model.objects` because that is what a
    migration's historical models expose. Rather than teach it about the two
    manager names, this swaps the manager out at the seam -- so there is
    exactly one place in the command where row-level security is bypassed, and
    it is named.
    """

    def __init__(self, model):
        self._model = model
        self.objects = model.all_objects

    def __getattr__(self, item):
        return getattr(self._model, item)


class Command(BaseCommand):
    help = "Assign any Player/Match/ScheduledMatch/Championship/Location with a null club to a club."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=club_backfill.DEFAULT_BATCH_SIZE,
            help=(
                "Rows per batch. Lower it if the VPS is under memory pressure "
                f"(default {club_backfill.DEFAULT_BATCH_SIZE})."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        if batch_size < 1:
            self.stderr.write(self.style.ERROR("--batch-size must be at least 1"))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN — nothing will be written")
            )

        report = club_backfill.run(
            command_models(), batch_size=batch_size, dry_run=dry_run
        )

        for line in report.lines():
            self.stdout.write(line)

        if report.anomalies:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(report.anomalies)} anomaly/anomalies logged above. "
                    "These rows were assigned, not skipped — review them, but "
                    "nothing is hidden from users."
                )
            )

        if report.total_updated == 0:
            self.stdout.write(
                self.style.SUCCESS("\nNothing to do — every row already has a club.")
            )
        elif dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\nWould update {report.total_updated} row(s). Re-run without "
                    "--dry-run to apply."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"\nUpdated {report.total_updated} row(s).")
            )

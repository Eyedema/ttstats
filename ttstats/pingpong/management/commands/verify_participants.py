"""Check that participant rows and winner_side agree with the Team FKs.

Guard rail for the Team -> MatchParticipant migration: run it after migrating
and after each read path is switched over. Exits non-zero when anything has
drifted, so it can gate a deploy.
"""
from django.core.management.base import BaseCommand

from pingpong.models import Game, Match, ScheduledMatch, Side


class Command(BaseCommand):
    help = "Verify MatchParticipant/winner_side agree with the legacy Team FKs"

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose-rows",
            action="store_true",
            help="Print every divergent row instead of just the counts",
        )

    def handle(self, *args, **options):
        show_rows = options["verbose_rows"]
        problems = []

        problems += self._check_participants(
            Match.all_objects.select_related("team1", "team2").prefetch_related(
                "participants", "team1__players", "team2__players"
            ),
            "match",
        )
        problems += self._check_participants(
            ScheduledMatch.all_objects.select_related(
                "team1", "team2"
            ).prefetch_related(
                "participants", "team1__players", "team2__players"
            ),
            "scheduled match",
        )
        problems += self._check_match_winners()
        problems += self._check_game_winners()

        if not problems:
            self.stdout.write(
                self.style.SUCCESS("OK: participants and winner_side are consistent")
            )
            return

        if show_rows:
            for problem in problems:
                self.stdout.write(self.style.ERROR(problem))
        else:
            for problem in problems[:20]:
                self.stdout.write(self.style.ERROR(problem))
            if len(problems) > 20:
                self.stdout.write(
                    self.style.ERROR(f"... and {len(problems) - 20} more")
                )

        raise SystemExit(1)

    def _check_participants(self, queryset, label):
        problems = []
        for obj in queryset:
            for side, team in ((Side.ONE, obj.team1), (Side.TWO, obj.team2)):
                expected = (
                    {p.pk for p in team.players.all()} if team is not None else set()
                )
                actual = {
                    p.player_id for p in obj.participants.all() if p.side == side
                }
                if expected != actual:
                    problems.append(
                        f"{label} {obj.pk} side {int(side)}: "
                        f"team has {sorted(expected)}, participants have {sorted(actual)}"
                    )
        return problems

    def _check_match_winners(self):
        problems = []
        for match in Match.all_objects.all():
            expected = self._side_for(
                match.winner_id, match.team1_id, match.team2_id
            )
            if match.winner_side != expected:
                problems.append(
                    f"match {match.pk}: winner_side={match.winner_side}, "
                    f"expected {expected} from winner_id={match.winner_id}"
                )
        return problems

    def _check_game_winners(self):
        problems = []
        for game in Game.all_objects.select_related("match"):
            expected = self._side_for(
                game.winner_id, game.match.team1_id, game.match.team2_id
            )
            if game.winner_side != expected:
                problems.append(
                    f"game {game.pk}: winner_side={game.winner_side}, "
                    f"expected {expected} from winner_id={game.winner_id}"
                )
        return problems

    @staticmethod
    def _side_for(winner_id, team1_id, team2_id):
        if winner_id is None:
            return None
        if winner_id == team1_id:
            return Side.ONE
        if winner_id == team2_id:
            return Side.TWO
        return None

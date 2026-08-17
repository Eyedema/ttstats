"""Seed a realistic-looking demo dataset for design and screenshot work.

    python manage.py seed_demo          # build it
    python manage.py seed_demo --wipe   # remove it again

Why this exists: seed_e2e creates exactly two players and one live match,
which is all the Playwright suite needs and almost nothing a designer can
work from. Every screen that matters here is a *density* problem -- a
leaderboard with one row, an empty championship table and a match list with
no history tell you nothing about whether a layout holds up.

The roster is fixed and obviously fictional, so this can be re-run
idempotently and nobody's real match history has to leave the machine to get
a screenshot.

PREFER A SEPARATE DATABASE FILE. Run it against your working db.sqlite3 and
the demo roster shares a leaderboard with whatever junk is already in there
("Player 0" through "Player 54", in this repo's case), which is exactly what
you do not want in a screenshot:

    export TTSTATS_SQLITE_NAME=$PWD/demo.sqlite3
    python manage.py migrate && python manage.py seed_demo

`--wipe` removes the roster and everything attached to it, so seeding into
your dev file is recoverable -- but starting clean is easier than proving a
wipe was complete.

DEBUG-only, like seed_e2e: it creates accounts with a published password.
"""

import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from pingpong import live_scoring as ls
from pingpong.models import (
    Championship, Game, Location, Match, MatchConfirmation, Player,
    ScheduledMatch, Side,
)
from pingpong.services import set_match_sides, set_scheduled_match_sides

DEMO_PASSWORD = "demo-local-only"

# Fixed so runs are reproducible and screenshots are comparable across a
# redesign. Names are deliberately fictional-sounding.
DEMO_PLAYERS = [
    ("Nora Vance", "nora", "normal"),
    ("Milo Petrov", "milo", "hard_rubber"),
    ("Ines Okafor", "ines", "normal"),
    ("Casper Lund", "casper", "normal"),
    ("Rhea Salvatierra", "rhea", "hard_rubber"),
    ("Otto Brandt", "otto", "normal"),
    ("Suri Nakamura", "suri", "normal"),
    ("Bo Adeyemi", "bo", "unknown"),
]

DEMO_LOCATIONS = ["The Garage", "Club Hall", "Office Basement"]

# Seeded so the same run produces the same league table every time. A
# screenshot set that reshuffles on every run is useless for before/after.
RANDOM_SEED = 20260818


class Command(BaseCommand):
    help = "Seed a realistic demo dataset for design work (DEBUG only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--wipe", action="store_true",
            help="Delete the demo data and exit.",
        )
        parser.add_argument(
            "--matches", type=int, default=48,
            help="How many historical matches to generate (default 48).",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo refuses to run with DEBUG=False.")

        if options["wipe"]:
            self._wipe()
            self.stdout.write(self.style.SUCCESS("Demo data removed."))
            return

        random.seed(RANDOM_SEED)

        # Confirming a match sends mail to every verified player who still has
        # to confirm. With the console backend that is hundreds of emails
        # dumped into the terminal, burying the command's own output. Nothing
        # here is testing email, so it goes to locmem and is discarded.
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
        ):
            self._wipe()
            players = self._create_players()
            locations = self._create_locations()
            self._create_history(players, locations, options["matches"])
            self._create_championship(players, locations)
            self._create_scheduled(players, locations)
            self._create_pending_confirmation(players, locations)
            self._create_live_match(players, locations)

        self._report(players)

    # -- teardown ---------------------------------------------------------

    def _wipe(self):
        """Remove only the demo roster, leaving any other local data alone."""
        usernames = [username for _, username, _ in DEMO_PLAYERS]
        players = Player.objects.filter(name__in=[n for n, _, _ in DEMO_PLAYERS])

        # Matches go first: MatchParticipant.player is CASCADE, so deleting
        # players out from under a match empties a side of it and leaves a
        # half-match behind that breaks the match list for everyone.
        Championship.all_objects.filter(name__startswith="Demo ").delete()
        match_ids = set(
            Match.all_objects.filter(participants__player__in=players)
            .values_list("pk", flat=True)
        )
        ScheduledMatch.all_objects.filter(participants__player__in=players).delete()
        Match.all_objects.filter(pk__in=match_ids).delete()
        players.delete()
        User.objects.filter(username__in=usernames).delete()

    # -- setup ------------------------------------------------------------

    def _create_players(self):
        players = []
        for name, username, style in DEMO_PLAYERS:
            user = User.objects.create_user(
                username=username,
                email=f"{username}@demo.invalid",
                password=DEMO_PASSWORD,
            )
            # Verified, so the UI shows the normal state rather than a
            # "confirm your email" banner in every screenshot.
            user.profile.email_verified = True
            user.profile.save()

            player = Player.objects.create(
                name=name, user=user, playing_style=style,
            )
            players.append(player)
        return players

    def _create_locations(self):
        return [Location.objects.get_or_create(name=n)[0] for n in DEMO_LOCATIONS]

    def _game_score(self):
        """A plausible finished-game scoreline, as (winner_points, loser_points).

        Deliberately NOT live_scoring.common_final_scores(): that returns only
        the shortcut presets (11-0 and 11-9), so every game would be either a
        shutout or a squeaker. Real matches are mostly in between.

        Validated against the app's own rule rather than trusted, so the demo
        can never contain a result GameForm would reject.
        """
        if random.random() < 0.15:
            # Deuce: past 11 the winner takes it by exactly two.
            loser = random.randint(10, 14)
            winner = loser + ls.MIN_LEAD
        else:
            loser = random.choice([2, 4, 5, 6, 7, 7, 8, 8, 9, 9])
            winner = ls.WIN_POINTS

        assert ls.is_valid_final_score(winner, loser), (winner, loser)
        return winner, loser

    def _play(self, match, stronger_side):
        """Fill in games until one side wins, favouring `stronger_side`.

        Note `stronger_side` is a models.Side (an int), not a
        live_scoring.Side (the string "team1"/"team2"). The two are different
        types with the same name; mixing them silently produces a KeyError
        rather than a wrong result, which is at least loud.
        """
        needed = ls.games_to_win(match.best_of)
        weaker = Side.TWO if stronger_side == Side.ONE else Side.ONE
        wins = {Side.ONE: 0, Side.TWO: 0}
        game_number = 1

        while max(wins.values()) < needed:
            # The favourite takes ~68% of games, which produces a league table
            # with real separation instead of everyone converging on 1500.
            winner = stronger_side if random.random() < 0.68 else weaker
            high, low = self._game_score()
            t1, t2 = (high, low) if winner == Side.ONE else (low, high)

            Game.objects.create(
                match=match, game_number=game_number,
                team1_score=t1, team2_score=t2,
                duration_minutes=random.randint(8, 22),
            )
            wins[winner] += 1
            game_number += 1

    def _confirm(self, match):
        """Confirm one player at a time so the Elo signals actually fire.

        bulk_create bypasses post_save, which is what maintains is_confirmed
        and triggers update_player_elo -- a bulk-created confirmation leaves
        the match looking played but rated 1500 forever.
        """
        for player in match.all_players:
            MatchConfirmation.objects.get_or_create(match=match, player=player)

    def _create_history(self, players, locations, count):
        """Historical singles and doubles, spread over the last ~16 weeks."""
        now = timezone.now()

        for i in range(count):
            days_ago = int((count - i) * (112 / max(count, 1)))
            played = now - timedelta(
                days=days_ago, hours=random.randint(0, 10)
            )

            is_double = (i % 7 == 0)
            if is_double:
                four = random.sample(players, 4)
                side1, side2 = four[:2], four[2:]
            else:
                two = random.sample(players, 2)
                side1, side2 = [two[0]], [two[1]]

            with transaction.atomic():
                match = Match.objects.create(
                    date_played=played,
                    best_of=random.choice([3, 5, 5, 7]),
                    match_type=random.choice(["casual", "casual", "practice"]),
                    location=random.choice(locations),
                    is_double=is_double,
                )
                set_match_sides(match, side1, side2)

                # Bias toward whichever side is currently rated higher, so
                # ratings reinforce rather than random-walk back to the mean.
                r1 = sum(p.elo_rating for p in side1) / len(side1)
                r2 = sum(p.elo_rating for p in side2) / len(side2)
                self._play(match, Side.ONE if r1 >= r2 else Side.TWO)
                self._confirm(match)

            for p in side1 + side2:
                p.refresh_from_db()

    def _create_championship(self, players, locations):
        """A championship mid-season: some rounds played, some still to come."""
        today = timezone.localdate()
        championship = Championship.objects.create(
            name="Demo Winter League",
            description="Round robin, home and away. Loser buys the coffee.",
            championship_type=Championship.ChampionshipType.SINGLES,
            max_participants=4,
            start_date=today - timedelta(days=21),
            end_date=today + timedelta(days=21),
            status=Championship.Status.REGISTRATION,
            location=locations[0],
            created_by=players[0],
        )
        for player in players[:4]:
            championship.register_entry([player])

        championship.generate_schedule()
        championship.status = Championship.Status.IN_PROGRESS
        championship.save()

        # Convert and play the first two thirds, leaving the rest scheduled --
        # a half-finished table is what the standings screen has to render,
        # and an all-played or all-empty one hides the hard case.
        scheduled = list(
            ScheduledMatch.all_objects.filter(championship=championship)
            .order_by("scheduled_date", "pk")
        )
        for sm in scheduled[: int(len(scheduled) * 0.6)]:
            side1 = list(sm.side1_players)
            side2 = list(sm.side2_players)
            if not side1 or not side2:
                continue

            with transaction.atomic():
                match = Match.objects.create(
                    date_played=timezone.make_aware(
                        timezone.datetime.combine(sm.scheduled_date, sm.scheduled_time)
                    ) if timezone.is_naive(
                        timezone.datetime.combine(sm.scheduled_date, sm.scheduled_time)
                    ) else timezone.datetime.combine(sm.scheduled_date, sm.scheduled_time),
                    best_of=5,
                    match_type="tournament",
                    location=championship.location,
                    championship=championship,
                )
                set_match_sides(match, side1, side2)
                self._play(match, Side.ONE if random.random() < 0.5 else Side.TWO)
                self._confirm(match)
            sm.delete()

    def _create_scheduled(self, players, locations):
        """Upcoming fixtures, so the calendar and dashboard are not empty."""
        today = timezone.localdate()
        for offset, (a, b) in enumerate(
            [(0, 1), (2, 3), (4, 5), (1, 6)], start=1
        ):
            sm = ScheduledMatch.objects.create(
                scheduled_date=today + timedelta(days=offset * 2),
                scheduled_time=timezone.datetime.strptime("19:30", "%H:%M").time(),
                location=random.choice(locations),
                notification_sent=True,
            )
            set_scheduled_match_sides(sm, [players[a]], [players[b]])

    def _create_pending_confirmation(self, players, locations):
        """One match played but NOT confirmed.

        The dashboard's whole call to action is "you have a result waiting",
        and with everything confirmed that state never appears in a
        screenshot.
        """
        match = Match.objects.create(
            date_played=timezone.now() - timedelta(hours=3),
            best_of=5,
            match_type="casual",
            location=locations[0],
        )
        set_match_sides(match, [players[0]], [players[1]])
        self._play(match, Side.ONE)

    def _create_live_match(self, players, locations):
        """A match in progress, parked mid-game.

        The live scoreboard is the app's best screen and the one a redesign
        most needs to see, and it only renders for a match with is_live=True
        and a populated live_state. Left at one game each and 8-6 in the
        third, which exercises the parts a 0-0 board hides: a game-count row
        with real numbers, a two-digit-adjacent score, and the deciding-game
        state.
        """
        match = Match.objects.create(
            date_played=timezone.now(),
            best_of=5,
            match_type="casual",
            location=locations[0],
            is_live=True,
            scorekeeper=players[0],
        )
        set_match_sides(match, [players[0]], [players[1]])

        state = ls.initial_state(match.best_of)
        state = ls.set_initial_server(state, "team1")

        # Drive it through the real rules rather than hand-writing the JSON:
        # a state this app never actually produced would be a lie in exactly
        # the place a designer would trust it.
        def score(sequence):
            nonlocal state
            for side in sequence:
                state, _ = ls.apply_point(state, side)

        def game(winner, loser_points):
            """Point order that lands a game on exactly 11-`loser_points`.

            The points have to be interleaved, not stacked: apply_point ends
            the game the instant a side reaches 11, so eleven straight points
            finishes it at 11-0 and every remaining point in the sequence
            silently lands in the NEXT game.
            """
            loser = ls.other(winner)
            seq = []
            for _ in range(loser_points):
                seq.extend([loser, winner])
            seq.extend([winner] * (ls.WIN_POINTS - loser_points))
            return seq

        # Game 1 to team1 (11-5), game 2 to team2 (11-8) ...
        score(game("team1", 5))
        score(game("team2", 8))
        # ... then park the decider at 8-6, mid-rally.
        score(["team1", "team2"] * 6 + ["team1"] * 2)

        Match.all_objects.filter(pk=match.pk).update(live_state=state)

    # -- output -----------------------------------------------------------

    def _report(self, players):
        # Refresh BEFORE sorting: these objects were loaded before any match
        # was played, so their in-memory elo_rating is still 1500 and sorting
        # on it produces a table that is ordered by nothing at all while
        # printing the correct (refreshed) numbers -- which reads as a bug in
        # the Elo system rather than in this method.
        for p in players:
            p.refresh_from_db()
        ranked = sorted(players, key=lambda p: -p.elo_rating)

        # Scoped to the demo roster. Counting every row in the database
        # reports whatever else happens to be in your dev file and makes it
        # look like the seed generated far more than it did.
        demo_matches = Match.all_objects.filter(
            participants__player__in=players
        ).distinct().count()
        demo_scheduled = ScheduledMatch.all_objects.filter(
            participants__player__in=players
        ).distinct().count()

        self.stdout.write(self.style.SUCCESS("\nDemo data seeded.\n"))
        self.stdout.write(f"  matches:      {demo_matches}")
        self.stdout.write(f"  scheduled:    {demo_scheduled}")
        self.stdout.write("  championship: Demo Winter League (in progress)\n")
        self.stdout.write("  leaderboard:")
        for i, p in enumerate(ranked, start=1):
            self.stdout.write(
                f"    {i}. {p.name:<20} {p.elo_rating}  ({p.matches_for_elo} rated)"
            )
        self.stdout.write(
            f"\n  log in as any of: "
            f"{', '.join(u for _, u, _ in DEMO_PLAYERS)}"
            f"  /  password: {DEMO_PASSWORD}\n"
        )

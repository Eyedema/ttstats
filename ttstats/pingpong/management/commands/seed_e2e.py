"""Seed a deterministic fixture for the Playwright end-to-end suite.

The e2e suite drives the real app in a real browser engine, so it needs a
known user and a known live match to point at. Refuses to run unless DEBUG is
on, because it creates an account with a published password.
"""
from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from pingpong import live_scoring as ls, point_rollups, services
from pingpong.models import (
    Club, ClubInvite, ClubMembership, ClubRole, FeatureFlagOverride, Game, LiveLobby,
    LiveScorer, Match, MatchConfirmation, Player, PlayerRating, Season, SeasonStanding, Side,
)

E2E_USERNAME = "e2e"
E2E_PASSWORD = "e2e-local-only"
E2E_OPPONENT = "e2e-opponent"
E2E_MOMENTUM_OPPONENT = "e2e-momentum"
# The seasons user (KAN-48/50): same password, a member with season results,
# for the season leaderboard and archive specs.
E2E_SEASONS_USERNAME = "e2e-seasons"
# The Glicko-2 user (KAN-71/72): Glicko state written by the real pipeline and
# a result awaiting their confirmation, for the rating-component specs.
E2E_GLICKO_USERNAME = "e2e-glicko"
# The network-chaos opponent (KAN-83): every offline spec creates and finishes
# its own live match against this player, so those results never touch
# E2E Opponent's, which the season and Glicko specs read.
E2E_CHAOS_OPPONENT = "e2e-chaos"
# Shared scoring (KAN-153): the guest scorer of a seeded two-phone match, so
# the scoreboard's "my side" layout can be opened without the join flow. Since
# KAN-155 it also logs in (same password, its own storageState) as the second
# phone of the two-context specs, and is a member of the e2e club so it may
# join the host's lobbies.
E2E_PARTNER = "e2e-partner"
# KAN-155: a lobby whose link has expired, at a fixed token, so the dead-link
# spec can open it without waiting out LOBBY_TTL. Hosted by E2E Opponent so
# E2E Player's own lobbies (one open per host) never touch it.
E2E_EXPIRED_LOBBY_TOKEN = "e2e0expired0lobby000000000000000"
# Every player the seed rates. A re-seed starts them from scratch.
E2E_PLAYER_NAMES = (
    "E2E Player", "E2E Opponent", "E2E Momentum", "E2E Seasons Player",
    "E2E Glicko Player", "E2E Chaos", "E2E Partner",
)


class Command(BaseCommand):
    help = "Create the fixture the Playwright suite expects (DEBUG only)."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_e2e refuses to run with DEBUG=False.")

        # Idempotent: the suite re-seeds before every run. A live match left
        # over from a previous run would make the scoreboard specs start from
        # a non-zero score, and a finished one (or Elo applied on top of the
        # last run's) would move every rating the season specs assert.
        self._reset_seeded_players()

        user, _ = User.objects.get_or_create(
            username=E2E_USERNAME, defaults={"email": "e2e@example.test"}
        )
        user.set_password(E2E_PASSWORD)
        user.save()
        user.profile.email_verified = True
        user.profile.save()

        player, _ = Player.objects.get_or_create(name="E2E Player")
        player.user = user
        player.save()

        opponent_user, _ = User.objects.get_or_create(
            username=E2E_OPPONENT, defaults={"email": "e2e-opp@example.test"}
        )
        opponent_user.profile.email_verified = True
        opponent_user.profile.save()
        opponent, _ = Player.objects.get_or_create(name="E2E Opponent")
        opponent.user = opponent_user
        opponent.save()

        match = Match.all_objects.create(
            best_of=5,
            is_live=True,
            scorekeeper=player,
            live_state=ls.initial_state(5),
        )
        services.set_match_sides(match, [player], [opponent])

        momentum_match = self._seed_completed_live_match(player)

        chaos_user, _ = User.objects.get_or_create(
            username=E2E_CHAOS_OPPONENT, defaults={"email": "e2e-chaos@example.test"}
        )
        chaos_user.profile.email_verified = True
        chaos_user.profile.save()
        chaos, _ = Player.objects.get_or_create(name="E2E Chaos")
        chaos.user = chaos_user
        chaos.save()

        # A club, so the invite specs have something to point at. Idempotent
        # like everything else here: the suite re-seeds before every run.
        club, _ = Club.objects.get_or_create(
            slug="e2e-club", defaults={"name": "E2E Club"}
        )
        ClubMembership.objects.get_or_create(
            club=club, user=user, defaults={"role": ClubRole.OWNER}
        )
        Player.objects.filter(
            pk__in=[player.pk, opponent.pk, momentum_match.opponent_pk, chaos.pk]
        ).update(club=club)
        Match.all_objects.filter(
            pk__in=[match.pk, momentum_match.pk]
        ).update(club=club)

        # Shared scoring (KAN-151) is flag-gated; the seeded user gets it, the
        # way production rolls it out (a per-user override first).
        FeatureFlagOverride.objects.update_or_create(
            name="shared_scoring", user=user, defaults={"enabled": True, "note": "e2e"}
        )

        # A second club, so the switcher actually has something to switch
        # between. A single-club user sees no switcher by design, which would
        # make the S1.8 specs assert on markup that is correctly absent.
        second, _ = Club.objects.get_or_create(
            slug="e2e-club-two",
            defaults={"name": "E2E Second Club With A Deliberately Long Name"},
        )
        ClubMembership.objects.get_or_create(
            club=second, user=user, defaults={"role": ClubRole.MEMBER}
        )

        shared_match = self._seed_shared_live_match(club, player)
        self._seed_expired_lobby(club, opponent)

        season = self._seed_season(club, player, opponent)
        glicko_match = self._seed_glicko(club, opponent)

        # One live invite and one revoked one, so the suite can render both the
        # working and the dead page without mutating state mid-run.
        ClubInvite.objects.filter(club=club).delete()
        live_invite = ClubInvite.issue(club, user, max_uses=50, note="e2e")
        dead_invite = ClubInvite.issue(club, user, note="e2e revoked")
        dead_invite.revoke()

        archived_season = self._seed_archived_season(club, user, player, opponent)

        # The suite reads these off stdout rather than hard-coding a pk.
        self.stdout.write(f"E2E_USERNAME={E2E_USERNAME}")
        self.stdout.write(f"E2E_PASSWORD={E2E_PASSWORD}")
        self.stdout.write(f"E2E_MATCH_PK={match.pk}")
        self.stdout.write(f"E2E_MOMENTUM_MATCH_PK={momentum_match.pk}")
        self.stdout.write(f"E2E_SHARED_MATCH_PK={shared_match.pk}")
        self.stdout.write(f"E2E_INVITE_TOKEN={live_invite.token}")
        self.stdout.write(f"E2E_REVOKED_INVITE_TOKEN={dead_invite.token}")
        self.stdout.write(f"E2E_CLUB={club.name}")
        self.stdout.write(f"E2E_CLUB_TWO={second.name}")
        self.stdout.write(f"E2E_SEASONS_USERNAME={E2E_SEASONS_USERNAME}")
        self.stdout.write(f"E2E_SEASON={season.name}")
        self.stdout.write(f"E2E_GLICKO_USERNAME={E2E_GLICKO_USERNAME}")
        self.stdout.write(f"E2E_GLICKO_PENDING_MATCH_PK={glicko_match.pk}")
        self.stdout.write(f"E2E_ARCHIVED_SEASON_PK={archived_season.pk}")

    def _reset_seeded_players(self):
        """Back to a fresh database for the seeded players: no matches, no ratings.

        Every match involving them goes (live, finished, seeded or left behind
        by a spec), which cascades RatingChange; their PlayerRating rows go.
        Everything is then rebuilt by the real pipeline below, so a re-run
        writes exactly what the first run wrote. Overrides of the retired
        `seasons` / `glicko2` flags left by an older seed are dropped.
        """
        players = Player.objects.filter(name__in=E2E_PLAYER_NAMES)
        Match.all_objects.filter(participants__player__in=players).distinct().delete()
        PlayerRating.all_objects.filter(player__in=players).delete()
        FeatureFlagOverride.objects.filter(name__in=["seasons", "glicko2"]).delete()

    def _seed_shared_live_match(self, club, player):
        """A live match scored on two phones (KAN-153): E2E Player is the
        host on side 1, E2E Partner the guest on side 2, each restricted to
        their own side -- the shape the join flow (KAN-152) leaves behind.
        Not started, so the specs pick a server and never assume a score."""
        partner_user, _ = User.objects.get_or_create(
            username=E2E_PARTNER, defaults={"email": "e2e-partner@example.test"}
        )
        partner_user.set_password(E2E_PASSWORD)
        partner_user.save()
        partner_user.profile.email_verified = True
        partner_user.profile.save()
        ClubMembership.objects.get_or_create(
            club=club, user=partner_user, defaults={"role": ClubRole.MEMBER}
        )
        partner, _ = Player.objects.get_or_create(name="E2E Partner")
        partner.user = partner_user
        partner.club = club
        partner.save()
        match = services.create_live_match(
            Match(best_of=5, club=club),
            scorekeeper=player,
            side1_players=[player],
            side2_players=[partner],
            can_score_both=False,
        )
        LiveScorer.objects.create(
            match=match, player=partner, side=Side.TWO, role=LiveScorer.Role.GUEST
        )
        return match

    def _seed_expired_lobby(self, club, host):
        """A lobby past its expiry at a known token (KAN-155's dead link)."""
        LiveLobby.all_objects.filter(token=E2E_EXPIRED_LOBBY_TOKEN).delete()
        now = timezone.now()
        LiveLobby.all_objects.create(
            token=E2E_EXPIRED_LOBBY_TOKEN,
            host=host,
            club=club,
            best_of=3,
            status=LiveLobby.Status.EXPIRED,
            expires_at=now - timedelta(hours=1),
        )

    def _seed_season(self, club, player, opponent):
        """A current season with confirmed singles results, for the season table.

        Matches are confirmed through the real pipeline (MatchConfirmation ->
        recompute -> apply_match_ratings), so the PlayerRating and RatingChange
        rows the specs read are the ones the app writes. Everyone ends up
        provisional (a handful of matches), and E2E Momentum -- rated
        all-time from the momentum match, which predates the club -- is
        unrated this season. Doubles has nobody, so its empty state renders.
        """
        today = timezone.now().date()
        # update_or_create: a season seeded on an earlier day is moved to
        # cover today, so a current season always exists.
        season, _ = Season.all_objects.update_or_create(
            club=club,
            name="E2E Season",
            defaults={
                "starts_on": today - timedelta(days=30),
                "ends_on": today + timedelta(days=60),
            },
        )

        user, _ = User.objects.get_or_create(
            username=E2E_SEASONS_USERNAME,
            defaults={"email": "e2e-seasons@example.test"},
        )
        user.set_password(E2E_PASSWORD)
        user.save()
        user.profile.email_verified = True
        user.profile.save()
        me, _ = Player.objects.get_or_create(name="E2E Seasons Player")
        me.user = user
        me.club = club
        me.save()
        ClubMembership.objects.get_or_create(
            club=club, user=user, defaults={"role": ClubRole.MEMBER}
        )

        for winner, loser in ((me, opponent), (me, opponent), (player, opponent)):
            match = Match.all_objects.create(club=club, best_of=3)
            services.set_match_sides(match, [winner], [loser])
            for n in (1, 2):
                Game.objects.create(
                    match=match, game_number=n, team1_score=11, team2_score=6
                )
            for p in (winner, loser):
                MatchConfirmation.objects.get_or_create(match=match, player=p)
        return season

    def _seed_glicko(self, club, opponent):
        """The Glicko-2 member: two confirmed wins and one result awaiting them.

        Confirmed through the real pipeline, so the PlayerRating Glicko fields
        and the RatingChange history are the ones the app writes. Two matches
        leave a wide RD: the player is provisional. The last match is the
        opponent's win with only the opponent's confirmation, which is what
        the dashboard and match detail preview ("if agreed").
        """
        user, _ = User.objects.get_or_create(
            username=E2E_GLICKO_USERNAME,
            defaults={"email": "e2e-glicko@example.test"},
        )
        user.set_password(E2E_PASSWORD)
        user.save()
        user.profile.email_verified = True
        user.profile.save()
        me, _ = Player.objects.get_or_create(name="E2E Glicko Player")
        me.user = user
        me.club = club
        me.save()
        ClubMembership.objects.get_or_create(
            club=club, user=user, defaults={"role": ClubRole.MEMBER}
        )

        def play(winner, loser, confirm_by):
            match = Match.all_objects.create(club=club, best_of=3)
            services.set_match_sides(match, [winner], [loser])
            for n in (1, 2):
                Game.objects.create(
                    match=match, game_number=n, team1_score=11, team2_score=7
                )
            for p in confirm_by:
                MatchConfirmation.objects.get_or_create(match=match, player=p)
            return match

        play(me, opponent, [me, opponent])
        play(me, opponent, [me, opponent])
        return play(opponent, me, [opponent])

    def _seed_archived_season(self, club, user, player, opponent):
        """An archived season with a frozen table, for the archive specs (KAN-51).

        The specs run as the `e2e-seasons` user. The rows are written directly rather than through a rollover: the specs are about
        what the archive *renders*, and real matches here would move every
        other spec's leaderboard. Rollover itself is covered in pytest.

        The table carries a shared first place and a deleted player (no
        Player, only the frozen name), the two rows most likely to break.
        """
        SeasonStanding.all_objects.filter(season__club=club).delete()
        Season.all_objects.filter(club=club, name="E2E Spring 2025").delete()
        season = Season.all_objects.create(
            club=club, name="E2E Spring 2025",
            starts_on=date(2025, 1, 1), ends_on=date(2025, 3, 31),
            archived_at=timezone.now(),
        )
        rows = [
            ("singles", player, player.name, 1, 1532, 4, 3, 1),
            ("singles", opponent, opponent.name, 1, 1532, 4, 2, 2),
            ("singles", None, "E2E Departed", 3, 1468, 4, 1, 3),
            ("doubles", player, player.name, 1, 1516, 1, 1, 0),
        ]
        SeasonStanding.all_objects.bulk_create(
            SeasonStanding(
                season=season, format=fmt, player=who, player_name=name,
                rank=rank, rating=rating, matches=matches, wins=wins, losses=losses,
                notified_at=timezone.now(),
            )
            for fmt, who, name, rank, rating, matches, wins, losses in rows
        )
        return season

    def _seed_completed_live_match(self, player):
        """A FINISHED live-scored match, so the momentum specs have a chart.

        Driven through `live_scoring` and the app's own
        `views._record_point_event`, deliberately: a hand-written fixture of
        PointEvent rows would let the seed and the write path disagree, and
        the specs would then be asserting against a log the app cannot
        actually produce.

        The scoreline is chosen to exercise all three shapes on one page: a
        comeback from 5-9 down, a shutout, and a deuce game.
        """
        from pingpong.views import _record_point_event

        opponent_user, _ = User.objects.get_or_create(
            username=E2E_MOMENTUM_OPPONENT,
            defaults={"email": "e2e-momentum@example.test"},
        )
        opponent_user.profile.email_verified = True
        opponent_user.profile.save()
        opponent, _ = Player.objects.get_or_create(name="E2E Momentum")
        opponent.user = opponent_user
        opponent.save()

        match = Match.all_objects.create(
            best_of=3,
            is_live=True,
            scorekeeper=player,
            live_state=ls.set_initial_server(ls.initial_state(3), "team1"),
        )
        services.set_match_sides(match, [player], [opponent])

        script = (
            (["team1", "team2"] * 5) + ["team2"] * 4 + ["team1"] * 6  # 11-9
            + ["team2"] * 11                                          # 0-11
            + (["team1", "team2"] * 10) + ["team1"] * 2               # 12-10
        )
        for side in script:
            state_before = match.live_state
            new_state, completed = ls.apply_point(state_before, side)
            _record_point_event(
                match,
                state_before=state_before,
                side=side,
                completed=completed,
                new_state=new_state,
            )
            if completed is None:
                match.live_state = new_state
                continue

            if ls.is_match_complete(new_state):
                # Same order as LivePointView: is_live off before the final
                # Game row, so the normal winner/Elo pipeline runs.
                Match.all_objects.filter(pk=match.pk).update(
                    is_live=False, live_state=None
                )
                match.is_live = False
                match.live_state = None
            else:
                match.live_state = new_state

            Game.objects.create(
                match=match,
                game_number=completed["game_number"],
                team1_score=completed["team1_score"],
                team2_score=completed["team2_score"],
            )

        match.refresh_from_db()

        # Both sides confirm, so the match reaches the confirmed set the
        # head-to-head page works from. Without this the S2.7 comparison would
        # correctly render nothing and its spec would pass for the wrong
        # reason.
        for participant in match.participants.select_related("player"):
            MatchConfirmation.objects.get_or_create(
                match=match, player=participant.player
            )
        match.refresh_from_db()

        point_rollups.rebuild_match_summaries(match)
        match.opponent_pk = opponent.pk
        return match

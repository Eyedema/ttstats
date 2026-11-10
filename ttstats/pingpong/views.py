import json
import logging
from dataclasses import dataclass
from urllib.parse import urlencode
from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.urls import reverse_lazy
from datetime import timedelta

from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited

logger = logging.getLogger(__name__)

from ttstats.middleware import get_current_club

from . import live_ops, live_scoring, notifications, offline, player_hues, point_rollups, point_stats, push, rating_pipeline, ratings
from .forms import GameForm, MatchEditForm, MatchForm, PlayerRegistrationForm, ScheduledMatchForm, MatchConvertForm, \
    ChampionshipCreateForm, ChampionshipEditForm, ScheduledMatchEditForm, LeaderboardScopeForm, DEFAULT_TOP_X
from .achievements import get_achievement_progress
from . import cache_utils
from .managers import scope_to_active_club
from .models import Game, LiveScorer, Location, Match, MatchParticipant, Player, UserProfile, MatchConfirmation, ScheduledMatch, ScheduledMatchParticipant, Side, Championship, Club, ClubInvite, ClubMembership, ClubRole, format_side_label, NotificationKind, NotificationPreference, PushSubscription
from .emails import send_scheduled_match_email, send_passkey_deleted_email
from .services import create_live_match, set_match_sides, set_scheduled_match_sides

try:
    from django_otp_webauthn.models import WebAuthnCredential
except ImportError:
    WebAuthnCredential = None


def _parse_live_timestamp(value):
    """ISO string from Match.live_state -> datetime, or None.

    Returns None rather than raising on anything unparseable: a malformed
    timestamp in the scoreboard's JSON should cost you the "last point"
    line, not the whole dashboard.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def append_widget_class(field, css):
    """Add classes to a field's widget without discarding what is there.

    `widget.attrs.update({"class": ...})` replaces the attribute outright,
    which silently drops the shared `field-input` styling that
    StyledFormMixin applied.
    """
    existing = field.widget.attrs.get("class", "")
    field.widget.attrs["class"] = f"{existing} {css}".strip()


def participants_prefetch(lookup="participants", scheduled=False):
    """Prefetch participants with their player/user/profile in one go.

    Ordered by player name so cached_player1/2 match the old
    team.players.all() ordering (Player.Meta.ordering).
    """
    model = ScheduledMatchParticipant if scheduled else MatchParticipant
    return Prefetch(
        lookup,
        queryset=model.objects.select_related("player__user__profile").order_by(
            "player__name"
        ),
    )


def _player_side(player, match):
    """Which side a player is on, from prefetched participants."""
    for participant in match.participants.all():
        if participant.player_id == player.pk:
            return participant.side
    return None


def _player_won(player, match):
    return (
        match.winner_side is not None
        and match.winner_side == _player_side(player, match)
    )


def cache_side_players(obj):
    """Attach cached_team1_players / cached_team2_players (+ player1/2).

    Reads the prefetched participants, so it costs no extra queries.
    """
    participants = list(obj.participants.all())
    obj.cached_team1_players = [p.player for p in participants if p.side == Side.ONE]
    obj.cached_team2_players = [p.player for p in participants if p.side == Side.TWO]
    obj.cached_player1 = (
        obj.cached_team1_players[0] if obj.cached_team1_players else None
    )
    obj.cached_player2 = (
        obj.cached_team2_players[0] if obj.cached_team2_players else None
    )
    # Labels too: templates rendering {{ match.team1 }} hit Team.__str__, which
    # queries players once per row.
    obj.cached_side1_label = format_side_label(obj.cached_team1_players) or "Side 1"
    obj.cached_side2_label = format_side_label(obj.cached_team2_players) or "Side 2"
    return obj


# Create your views here.
class PlayerListView(LoginRequiredMixin, ListView):
    """View to list all players"""

    template_name = "pingpong/player_list.html"
    context_object_name = "players"
    model = Player
    paginate_by = 10


class MatchListView(LoginRequiredMixin, ListView):
    """View to list all matches"""

    template_name = "pingpong/match_list.html"
    context_object_name = "matches"
    model = Match
    paginate_by = 10

    def get_queryset(self):
        return (
            Match.objects.all()
            .select_related("location", "championship")
            .prefetch_related(
                participants_prefetch(),
                "games",
                "confirmations",
            )
            .order_by("-date_played")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Cache scores using prefetched games data to avoid N+1 queries
        # Games are already loaded via prefetch_related, so this is free
        page_obj = context.get('page_obj')
        if page_obj:
            matches = page_obj.object_list
        else:
            matches = context.get('matches', [])

        for match in matches:
            # Force evaluation of prefetched data and cache as lists
            # This prevents template from triggering new queries
            games = list(match.games.all())
            match.cached_team1_score = sum(
                1 for g in games if g.winner_side == Side.ONE
            )
            match.cached_team2_score = sum(
                1 for g in games if g.winner_side == Side.TWO
            )

            # Cache side players as lists to avoid queries in template
            cache_side_players(match)

            # Cache winner players and label
            if match.winner_side == Side.ONE:
                match.cached_winner_players = match.cached_team1_players
                match.cached_winner_label = match.cached_side1_label
            elif match.winner_side == Side.TWO:
                match.cached_winner_players = match.cached_team2_players
                match.cached_winner_label = match.cached_side2_label
            else:
                match.cached_winner_players = []
                match.cached_winner_label = ""

            # Cache match_confirmed status to avoid N+1 queries
            # This replicates the logic from Match.team1_confirmed and Match.team2_confirmed
            # but uses the already-prefetched data
            confirmations = list(match.confirmations.all())
            confirmed_ids = {c.id for c in confirmations}

            # Team 1 confirmation check
            team1_verified_players = [
                p for p in match.cached_team1_players
                if p.user and hasattr(p.user, 'profile') and p.user.profile.email_verified
            ]
            team1_ids = {p.id for p in team1_verified_players}
            team1_all_unverified = len(team1_verified_players) == 0
            team1_confirmed = (team1_ids.issubset(confirmed_ids)) or team1_all_unverified

            # Team 2 confirmation check
            team2_verified_players = [
                p for p in match.cached_team2_players
                if p.user and hasattr(p.user, 'profile') and p.user.profile.email_verified
            ]
            team2_ids = {p.id for p in team2_verified_players}
            team2_all_unverified = len(team2_verified_players) == 0
            team2_confirmed = (team2_ids.issubset(confirmed_ids)) or team2_all_unverified

            match.cached_match_confirmed = team1_confirmed and team2_confirmed

        # Add total count for stats display (not just paginated count)
        context['total_matches'] = self.get_queryset().count()

        return context


class MatchDetailView(LoginRequiredMixin, DetailView):
    """View to show details of a single match"""

    template_name = "pingpong/match_detail.html"
    context_object_name = "match"
    model = Match
    paginate_by = 10

    def get_queryset(self):
        return super().get_queryset().select_related("championship")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        match = self.get_object()

        # Win probability from the pre-match ratings in the match's scope.
        scope = ratings.display_scope(match)
        t1_pct, t2_pct = ratings.win_probability(
            match.side1_players, match.side2_players, scope=scope, match=match,
        )
        context['team1_win_pct'] = t1_pct
        context['team2_win_pct'] = t2_pct

        context.update(self._viewer_context(match, scope))
        context.update(self._rating_deltas(match, scope))
        context.update(self._point_context(match))
        game_rows = self._game_rows(match)
        context['game_rows'] = game_rows
        context['long_games_note'] = self._long_games_note(game_rows)

        return context

    @staticmethod
    def _rating_deltas(match, scope):
        """What each side's rating did, or will do once both sides agree.

        A match awaiting confirmation has no RatingChange yet, and showing
        nothing there is the wrong answer: the number is exactly what the
        viewer is being asked to agree to. So the page shows the projection,
        clearly marked as pending, and swaps to the recorded figure once it is
        real. Both come from the same arithmetic (`ratings.match_deltas`), so
        the pending number is never a different number.
        """
        side1_delta, side2_delta, pending = ratings.match_deltas(match, scope)
        deltas = {
            'side1_delta': side1_delta,
            'side2_delta': side2_delta,
            'delta_is_pending': pending,
        }
        if match.is_double:
            # Doubles are rated per player: show each player's own figure,
            # not the side mean, so every number equals what is written.
            by_player, _ = ratings.projected_changes_by_player(match, scope)
            side1, side2 = match.sides()
            deltas['side1_player_deltas'] = [(p, by_player.get(p.pk, 0)) for p in side1]
            deltas['side2_player_deltas'] = [(p, by_player.get(p.pk, 0)) for p in side2]
        return deltas

    def _viewer_context(self, match, scope):
        """Where the person reading this page stands in it.

        The old template worked this out inline with a chain of
        `{% if user.player in match.side1_players %}` branches duplicated
        across a desktop layout and a mobile one -- four copies of the same
        question, which is how they came to disagree about whether a confirmed
        side should still show a Confirm button.
        """
        # The rating scope this match moves (KAN-48): its season and format.
        rating = {'rating_scope': scope, 'scope_label': ratings.scope_label(scope)}

        player = getattr(self.request.user, 'player', None)
        if player is None:
            return {
                'viewer_side': None, 'viewer_can_confirm': False,
                'viewer_has_confirmed': False, **rating,
            }

        side = None
        if any(p.pk == player.pk for p in match.side1_players):
            side = Side.ONE
        elif any(p.pk == player.pk for p in match.side2_players):
            side = Side.TWO

        has_confirmed = (
            side is not None
            and MatchConfirmation.objects.filter(match=match, player=player).exists()
        )
        return {
            'viewer_side': side,
            'viewer_can_confirm': (
                side is not None and match.winner_side is not None and not has_confirmed
            ),
            'viewer_has_confirmed': has_confirmed,
            **rating,
        }

    @staticmethod
    def _point_context(match):
        """The momentum chart and run timeline, for a live-scored match only.

        Computed here rather than in a fourth duplicated template branch --
        the old template asked where the viewer stood in four places and they
        came to disagree, which is what `_viewer_context` exists to prevent.

        **Absent, not empty, when there is no point log.** Most matches in the
        database were typed in as scorelines and always will be; a "no data"
        box on every one of them would occupy space to say nothing. The
        template gates the whole section on `has_point_data`.

        One query. This reads the raw log rather than the S2.4 rollups on
        purpose: the chart needs the per-point progression, which a rollup
        deliberately does not store -- rollups exist so that *career* figures
        never scan raw rows, and one match's log is ~60-100 of them.
        """
        stats = point_rollups.match_stats(match)
        if not stats.has_data:
            return {"has_point_data": False}

        side1, side2 = match.sides()
        # The raw hue value, which is what player_hues.hue_hex exists for:
        # Chart.js resolves a CSS custom property to black, so a chart is one
        # of the few places allowed a literal colour. It is still the player's
        # own permanent hue, not a chart palette, so the two series are the
        # two people -- and hues are byte-identical in both themes.
        colours = [
            player_hues.hue_hex(side1[0].pk if side1 else None),
            player_hues.hue_hex(side2[0].pk if side2 else None),
        ]
        # Two players who happen to land on the same palette slot would draw
        # two indistinguishable lines. Nudge the second one along rather than
        # letting the chart lie about whose run it was.
        if colours[0] == colours[1]:
            fallback = (side2[0].pk if side2 else 0) or 0
            colours[1] = player_hues.hue_hex(fallback + 1)

        games = []
        for game in stats.games:
            games.append({
                "game_number": game.game_number,
                "labels": list(range(len(game.progression))),
                "side1": [score[0] for score in game.progression],
                "side2": [score[1] for score in game.progression],
                "complete": game.complete,
            })

        # Resolved ONCE from the memoized sides(). `match.side_label()` goes
        # through `players_on()`, which is a fresh queryset every call -- two
        # per game here, so a five-game match would have added ten queries to
        # the page for two strings.
        side_labels = {
            int(Side.ONE): format_side_label(side1) or "Side 1",
            int(Side.TWO): format_side_label(side2) or "Side 2",
        }

        timeline = []
        for game in stats.games:
            winner_label = (
                side_labels.get(game.winner_side) if game.winner_side else None
            )
            timeline.append({
                "game_number": game.game_number,
                "score": f"{game.team1_score}\u2013{game.team2_score}",
                "complete": game.complete,
                "winner_label": winner_label,
                "lead_changes": game.lead_changes,
                "deficit_overcome": game.deficit_overcome,
                # The comeback stated in words as well as drawn, because a
                # line going up does not say how far back it started.
                "comeback_note": (
                    f"{winner_label} came back from {game.deficit_overcome} "
                    f"point{'s' if game.deficit_overcome != 1 else ''} down"
                    if game.is_comeback and winner_label
                    else None
                ),
                "longest_run": max(game.longest_run.values()) if game.longest_run else 0,
                "longest_run_label": (
                    side_labels.get(max(game.longest_run, key=game.longest_run.get))
                    if game.longest_run and max(game.longest_run.values()) > 1
                    else None
                ),
            })

        return {
            "has_point_data": True,
            "point_chart_data": {
                "games": games,
                "colours": colours,
                "labels": [side_labels[int(Side.ONE)], side_labels[int(Side.TWO)]],
            },
            "point_timeline": timeline,
            "point_total": stats.total_points,
        }

    @staticmethod
    def _game_rows(match):
        """Per-game rows carrying the share of points side one took.

        The design replaces two score boxes with a single proportional bar,
        because two boxes only say who won -- which the scoreline already says
        -- while the bar says whether it was close. A 14-16 and a 3-11 are the
        same "loss" in boxes and obviously different here.
        """
        rows = []
        for game in match.games.all():
            total = (game.team1_score or 0) + (game.team2_score or 0)
            rows.append({
                'game': game,
                # Integer percent: the bar is 300px wide, so a fractional
                # percentage buys nothing and makes the style attribute noisy.
                'side1_share': round((game.team1_score or 0) / total * 100) if total else 50,
                'went_long': max(game.team1_score or 0, game.team2_score or 0) > live_scoring.WIN_POINTS,
            })
        return rows

    @staticmethod
    def _long_games_note(game_rows):
        """One editorial line under the games, or nothing.

        A 3-1 that contained two deuce games is a different match from a 3-1
        that did not, and the scoreline cannot say so. When no game went past
        11 there is nothing worth saying, so nothing is said -- a line that
        appears every time stops being read.
        """
        long_games = sum(1 for row in game_rows if row['went_long'])
        if not long_games or not game_rows:
            return ''
        return (
            f"{long_games} of {len(game_rows)} games went past "
            f"{live_scoring.WIN_POINTS}. It was closer than the scoreline."
        )


class PlayerDetailView(LoginRequiredMixin, DetailView):
    """View to show details of a single player"""

    template_name = "pingpong/player_detail.html"
    context_object_name = "player"
    model = Player

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        player = self.get_object()
        page = self.request.GET.get('page', 1)

        # Try cache for aggregate stats (10 minute TTL)
        # Namespaced by the player's own club: these are that player's stats,
        # not a per-viewer view of them.
        stats_cache_key = f'{cache_utils.club_prefix(player.club)}player_stats_{player.pk}'
        cached_stats = cache.get(stats_cache_key)

        if cached_stats is None:
            # Cache miss - fetch and compute stats
            # Use is_confirmed=True to filter at DB level
            all_matches = Match.objects.filter(
                participants__player=player,
                is_confirmed=True,
            ).prefetch_related(participants_prefetch()).order_by('-date_played')

            confirmed_matches = list(all_matches)

            total_matches = len(confirmed_matches)
            wins = len([m for m in confirmed_matches if _player_won(player, m)])
            losses = total_matches - wins
            streaks = self._calculate_streaks(confirmed_matches)

            cached_stats = {
                'total_matches': total_matches,
                'wins': wins,
                'losses': losses,
                'win_rate': (wins / total_matches * 100) if total_matches > 0 else 0,
                'current_streak': streaks['current_streak'],
                'streak_type': streaks['streak_type'],
                'longest_win_streak': streaks['longest_win_streak'],
                'longest_loss_streak': streaks['longest_loss_streak'],
            }
            cache.set(stats_cache_key, cached_stats, 600)

        # Always fetch paginated matches fresh (lightweight with DB-level filtering)
        confirmed_matches_qs = Match.objects.filter(
            participants__player=player,
            is_confirmed=True,
        ).prefetch_related(participants_prefetch()).order_by('-date_played')

        from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

        matches_per_page = 10
        paginator = Paginator(confirmed_matches_qs, matches_per_page)

        try:
            confirmed_matches_page = paginator.page(page)
        except PageNotAnInteger:
            confirmed_matches_page = paginator.page(1)
        except EmptyPage:
            confirmed_matches_page = paginator.page(paginator.num_pages)

        # Add p1_score, p2_score, and player_won to each match from player's perspective
        for match in confirmed_matches_page.object_list:
            cache_side_players(match)
            # Everything below is from the viewer's perspective: the template
            # must not re-derive it, because the legacy `match.player1` it used
            # to compare against no longer exists and silently reads as empty.
            if _player_side(player, match) == Side.ONE:
                match.p1_score = match.team1_score
                match.p2_score = match.team2_score
                match.opponent_players = match.cached_team2_players
                match.opponent_label = match.cached_side2_label
            else:
                match.p1_score = match.team2_score
                match.p2_score = match.team1_score
                match.opponent_players = match.cached_team1_players
                match.opponent_label = match.cached_side1_label
            match.opponent_player = (
                match.opponent_players[0] if match.opponent_players else None
            )

            # Check if player won (works for both 1v1 and 2v2)
            match.player_won = _player_won(player, match)

        # Achievement data
        achievement_progress = get_achievement_progress(player)

        context.update(self._point_stats_context(player))

        # Per-format ratings with their provisional/unrated state (KAN-48/50).
        context['format_ratings'] = ratings.player_format_ratings(player)

        context.update({
            'matches': confirmed_matches_page.object_list,
            'page_obj': confirmed_matches_page,
            'is_paginated': paginator.num_pages > 1,
            'achievement_progress': achievement_progress,
            **cached_stats,
        })
        history = ratings.rating_history(player)
        context.update({
            'rating_chart_labels': history['labels'],
            'rating_chart_data': history['data'],
            'rating_chart_lower': history['lower'],
            'rating_chart_upper': history['upper'],
        })

        return context

    @staticmethod
    def _point_stats_context(player):
        """The player's career point-level record (KAN-32 / S2.6).

        Reads the S2.4 rollups, never the raw point rows: one aggregate query
        on a cache miss and none on a hit, against tens of thousands of point
        rows for the naive version.

        **Absent when the player has never been scored live**, which is most
        players today. A section of zeroes is worse than no section: it invites
        the reader to conclude something about a player it knows nothing about.

        Every rate is shown with the counts it came from, so the number can be
        verified rather than trusted -- and a rate off too small a sample is
        marked, not hidden (see point_stats.MIN_SAMPLE_POINTS).
        """
        stats = point_rollups.player_point_stats(player)
        if not stats.has_sample:
            return {"has_point_stats": False}

        return {
            "has_point_stats": True,
            "point_stats": stats,
            "point_serve_confident": stats.serve.is_confident(),
            "point_receive_confident": stats.receive.is_confident(),
            "point_deuce_confident": stats.deuce.is_confident(
                point_stats.MIN_SAMPLE_GAMES
            ),
            "point_min_sample": point_stats.MIN_SAMPLE_POINTS,
            "point_min_sample_games": point_stats.MIN_SAMPLE_GAMES,
            "point_comeback_deficit": point_stats.COMEBACK_DEFICIT,
        }

    def _calculate_streaks(self, matches):
        current_streak = streak_type = 0
        longest_win = longest_loss = win_streak = loss_streak = 0

        for match in matches:
            player_won = _player_won(self.object, match)

            if player_won:
                if streak_type != 'win':
                    longest_loss = max(longest_loss, loss_streak)
                    loss_streak = win_streak = 0
                    streak_type = 'win'
                win_streak += 1
                current_streak = win_streak
            elif match.winner_side:  # Loss
                if streak_type != 'loss':
                    longest_win = max(longest_win, win_streak)
                    win_streak = loss_streak = 0
                    streak_type = 'loss'
                loss_streak += 1
                current_streak = loss_streak

        if streak_type == 'win':
            longest_win = max(longest_win, win_streak)
        elif streak_type == 'loss':
            longest_loss = max(longest_loss, loss_streak)

        return {
            'current_streak': current_streak,
            'streak_type': streak_type,
            'longest_win_streak': longest_win,
            'longest_loss_streak': longest_loss,
        }


class DashboardView(LoginRequiredMixin, TemplateView):
    """Today: the three things that might need you, in that order.

    The old dashboard led with Total Matches and Active Players. Neither is
    something you can act on, and neither changes between two visits a minute
    apart, so they are gone. What replaces them is ranked by whether it needs
    you: a match you are scoring right now, a result somebody is waiting for
    you to agree to, then the rivalries and fixtures that are merely
    interesting.
    """

    template_name = "pingpong/dashboard.html"

    # How many of each block to show. Past this, the block hands off to its
    # own screen rather than growing -- a dashboard that scrolls is a list.
    CONFIRMATION_LIMIT = 4
    RIVALRY_LIMIT = 3
    FIXTURE_LIMIT = 3

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = getattr(self.request.user, "player", None)

        context.update(
            {
                "live_matches": self._live_matches(player),
                "pending_confirmations": self._pending_confirmations(player),
                "rivalries": self._rivalries(player),
                "fixtures": self._fixtures(player),
                "recent_matches": self._recent_matches(),
            }
        )
        return context

    # --- Live now -----------------------------------------------------------

    def _live_matches(self, player):
        """Matches this user is scorekeeping right now.

        Never cached: amber means live *this second*, and the whole point of
        the block is that the score is current.
        """
        if player is None:
            return []

        live_qs = Match.live_objects.filter(
            scorekeeper=player, is_live=True
        ).prefetch_related(participants_prefetch())

        live = []
        for m in live_qs:
            state = m.live_state or {}
            cache_side_players(m)
            opponents = [p for p in m.cached_team2_players]
            live.append(
                {
                    "pk": m.pk,
                    "opponent": " & ".join(p.name for p in opponents),
                    "opponent_player": opponents[0] if opponents else None,
                    "team1_label": " & ".join(p.name for p in m.cached_team1_players),
                    "team1_games": state.get("team1_games", 0),
                    "team2_games": state.get("team2_games", 0),
                    "team1_points": state.get("team1_points", 0),
                    "team2_points": state.get("team2_points", 0),
                    "game_number": state.get("team1_games", 0) + state.get("team2_games", 0) + 1,
                    # Parsed to a datetime here rather than passed through as
                    # the raw JSON string. live_state stores it as ISO text,
                    # and Django's date/timesince filters silently no-op on a
                    # string -- so the banner rendered the full
                    # "2026-08-17T23:15:21.959836+00:00" at the user.
                    "last_point_at": _parse_live_timestamp(
                        state.get("last_point_at")
                    ),
                    "best_of": m.best_of,
                    "resume_url": reverse("pingpong:live_scoreboard", args=[m.pk]),
                }
            )
        return live

    # --- Waiting on you -----------------------------------------------------

    def _pending_confirmations(self, player):
        """Finished matches this player has not yet agreed to.

        Only matches *they* still have to act on: a match waiting on the other
        player is not waiting on you, and putting it in a block headed "waiting
        on you" is how a dashboard teaches people to ignore it.
        """
        if player is None:
            return []

        qs = (
            Match.objects.filter(
                participants__player=player,
                winner_side__isnull=False,
                is_confirmed=False,
            )
            .exclude(matchconfirmation__player=player)
            .distinct()
            .prefetch_related(participants_prefetch())
            .order_by("-date_played")[: self.CONFIRMATION_LIMIT]
        )

        # "If true" is the season-and-format change that confirmation will
        # write (ratings.display_scope).
        rows = []
        for match in qs:
            cache_side_players(match)
            mine, theirs = self._sides_relative_to(match, player)
            opponent = theirs[0] if theirs else None
            delta_if_true = ratings.projected_change_for(
                match, player, ratings.display_scope(match)
            )
            rows.append(
                {
                    "match": match,
                    "opponent": opponent,
                    "opponent_label": " & ".join(p.name for p in theirs) or "Unknown",
                    "my_score": match.team1_score_cache
                    if self._is_side_one(match, player)
                    else match.team2_score_cache,
                    "their_score": match.team2_score_cache
                    if self._is_side_one(match, player)
                    else match.team1_score_cache,
                    # Signed, and shown before you agree: this is what saying
                    # yes costs. It is the same arithmetic that will be
                    # written when both of you have confirmed.
                    "delta_if_true": delta_if_true,
                    "waiting_since": match.date_played,
                }
            )
        return rows

    # --- Live rivalries -----------------------------------------------------

    def _rivalries(self, player):
        """The people you actually play, with the head-to-head that stings.

        Ordered by how recently you played them rather than by volume: a
        rivalry is live because it is ongoing, not because it was busy in
        2024.
        """
        if player is None:
            return []

        recent = (
            Match.objects.filter(
                participants__player=player,
                is_confirmed=True,
                winner_side__isnull=False,
            )
            .distinct()
            .prefetch_related(participants_prefetch())
            .order_by("-date_played")[:40]
        )

        # {opponent_pk: {"player":, "wins":, "losses":, "last":}}
        tally = {}
        for match in recent:
            cache_side_players(match)
            mine, theirs = self._sides_relative_to(match, player)
            if not theirs:
                continue
            i_won = self._is_side_one(match, player) == (match.winner_side == Side.ONE)
            for opponent in theirs:
                row = tally.setdefault(
                    opponent.pk,
                    {"player": opponent, "wins": 0, "losses": 0, "last": match.date_played},
                )
                row["wins" if i_won else "losses"] += 1
                row["last"] = max(row["last"], match.date_played)

        rows = sorted(tally.values(), key=lambda r: r["last"], reverse=True)
        for row in rows:
            row["played"] = row["wins"] + row["losses"]
            row["leading"] = row["wins"] > row["losses"]
        return rows[: self.RIVALRY_LIMIT]

    # --- On the calendar ----------------------------------------------------

    def _fixtures(self, player):
        """Your next few scheduled matches, unconverted ones only."""
        if player is None:
            return []

        today = timezone.localdate()
        qs = (
            ScheduledMatch.objects.filter(
                participants__player=player,
                scheduled_date__gte=today,
                match__isnull=True,
            )
            .distinct()
            .prefetch_related("participants__player")
            .order_by("scheduled_date", "scheduled_time")[: self.FIXTURE_LIMIT]
        )

        rows = []
        for sm in qs:
            theirs = [
                p.player
                for p in sm.participants.all()
                if p.player_id != player.pk
            ]
            rows.append(
                {
                    "scheduled_match": sm,
                    "opponent": theirs[0] if theirs else None,
                    "opponent_label": " & ".join(p.name for p in theirs) or "TBC",
                    "is_championship": sm.championship_id is not None,
                }
            )
        return rows

    # --- Shared helpers -----------------------------------------------------

    def _recent_matches(self):
        # Holds Match instances from a club-scoped query, so the key must carry
        # the club or the first club to load the dashboard serves every other.
        key = f"{cache_utils.club_prefix()}dashboard_recent_matches"
        recent_matches = cache.get(key)
        if recent_matches is None:
            recent_matches = list(Match.objects.all().order_by("-date_played")[:5])
            cache.set(key, recent_matches, 300)
        return recent_matches

    @staticmethod
    def _is_side_one(match, player):
        return any(p.pk == player.pk for p in match.cached_team1_players)

    @classmethod
    def _sides_relative_to(cls, match, player):
        """(my side's players, their side's players) -- never (side1, side2).

        Every row on this screen is written from the reader's point of view
        ("Casper says he beat you 3-1"), so the template must never have to
        work out which column it is in.
        """
        if cls._is_side_one(match, player):
            return list(match.cached_team1_players), list(match.cached_team2_players)
        return list(match.cached_team2_players), list(match.cached_team1_players)


class PlayView(LoginRequiredMixin, TemplateView):
    """Play: the only screen in the app that starts something.

    Play is a tab because starting a match is the app's reason to exist, and a
    button rather than a list because there are exactly three ways to begin
    one. It holds no data and makes no queries -- it is a fork in the road.
    """

    template_name = "pingpong/play.html"

class PlayerCreateView(LoginRequiredMixin, CreateView):
    """View to create a new player"""

    model = Player
    template_name = "pingpong/player_form.html"
    fields = ["name", "nickname", "playing_style", "notes"]
    success_url = reverse_lazy("pingpong:player_list")

    def form_valid(self, form):
        messages.success(
            self.request, f"Player '{form.instance.name}' created successfully!"
        )
        return super().form_valid(form)


class PlayerUpdateView(LoginRequiredMixin, UpdateView):
    """View to update an existing player"""

    model = Player
    template_name = "pingpong/player_form.html"
    fields = ["name", "nickname", "playing_style", "notes"]

    def get_success_url(self):
        return reverse_lazy("pingpong:player_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        messages.success(
            self.request, f"Player '{form.instance.name}' updated successfully!"
        )
        return super().form_valid(form)


class GameCreateView(LoginRequiredMixin, CreateView):
    """View to create a new game within a match"""

    model = Game
    form_class = GameForm
    template_name = "pingpong/game_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.match = get_object_or_404(Match, pk=kwargs["match_pk"])

        if request.user.is_authenticated and not self.match.user_can_edit(request.user):
            raise PermissionDenied

        # Check if match is already complete
        if self.match.winner_side:
            messages.warning(
                request, f"This match is already complete. {self.match.winner_label} won!"
            )
            return redirect("pingpong:match_detail", pk=self.match.pk)

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["match"] = self.match
        # The Alt+1..4 shortcut scorelines, derived from the scoring rules
        # rather than typed into the template's JavaScript.
        context["score_presets"] = live_scoring.common_final_scores()
        # Get next game number
        last_game = self.match.games.order_by("-game_number").first()
        context["next_game_number"] = (last_game.game_number + 1) if last_game else 1
        return context

    def form_valid(self, form):
        form.instance.match = self.match
        # Auto-set game_number if not provided
        if not form.instance.game_number:
            last_game = self.match.games.order_by("-game_number").first()
            form.instance.game_number = (last_game.game_number + 1) if last_game else 1

        # Save the game
        self.object = form.save()

        messages.success(
            self.request, f"Game {self.object.game_number} added successfully!"
        )

        # Refresh match from database to get updated winner status
        self.match.refresh_from_db()

        # Check if match is now complete
        if self.match.winner_side:
            # Check if it was auto-confirmed by signals.py logic
            if self.match.match_confirmed:
                unverified_players = self.match.get_unverified_players()
                if unverified_players:
                    messages.warning(
                        self.request,
                        f"Match auto-confirmed because {', '.join([p.name for p in unverified_players])} {'is an' if len(unverified_players) == 1 else 'are'} unverified user{'s' if len(unverified_players) > 1 else ''}.",
                    )
            messages.success(
                self.request,
                f"🎉 Match Complete! {self.match.winner_label} wins "
                f"{self.match.side1_score}-{self.match.side2_score}!",
            )
            # Always go to match detail if match is complete, regardless of button pressed
            return redirect("pingpong:match_detail", pk=self.match.pk)

        # Check if user wants to add another game (only if match not complete)
        if "add_another" in self.request.POST:
            # Redirect to add another game for the same match
            return redirect("pingpong:game_add", match_pk=self.match.pk)
        else:
            # Redirect to match detail
            return redirect("pingpong:match_detail", pk=self.match.pk)

    def get_success_url(self):
        # This won't be called since we're handling redirects in form_valid
        return reverse_lazy("pingpong:match_detail", kwargs={"pk": self.match.pk})


class MatchCreateView(LoginRequiredMixin, CreateView):
    """View to create a new match"""

    model = Match
    form_class = MatchForm
    template_name = "pingpong/match_form.html"

    def get_success_url(self):
        # "Score live" branch on form_valid sets self._start_live and creates
        # the match with is_live=True — jump straight into the scoreboard.
        if getattr(self, "_start_live", False):
            return reverse_lazy("pingpong:live_scoreboard", kwargs={"pk": self.object.pk})
        return reverse_lazy("pingpong:match_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["locations"] = Location.objects.all()

        # Add user permission info for template
        context["is_staff"] = self.request.user.is_staff
        try:
            context["user_player"] = self.request.user.player
        except AttributeError:
            context["user_player"] = None

        return context

    def get_form(self, form_class=None):
        """Customize form based on user permissions"""
        form = super().get_form(form_class)
        user = self.request.user

        if not user.is_staff:
            # Non-staff users must be one of the players
            try:
                user_player = user.player

                # Pre-select and lock user as player1
                form.fields["player1"].initial = user_player
                form.fields["player1"].disabled = True
                append_widget_class(
                    form.fields["player1"], "bg-muted cursor-not-allowed"
                )
                form.fields[
                    "player1"
                ].help_text = "You are automatically set as Player 1"

                # Limit player2 choices to exclude the user
                form.fields["player2"].queryset = Player.objects.exclude(
                    pk=user_player.pk
                )

                # Limit player3 choices to exclude the user
                form.fields["player3"].queryset = Player.objects.exclude(
                    pk=user_player.pk
                )

                # Limit player4 choices to exclude the user
                form.fields["player4"].queryset = Player.objects.exclude(
                    pk=user_player.pk
                )

            except Player.DoesNotExist:
                # User has no player profile - show error message
                messages.error(
                    self.request,
                    "You must have a player profile to create matches. Please contact an administrator.",
                )
                form.fields["player1"].disabled = True
                form.fields["player2"].disabled = True
                form.fields["player3"].disabled = True
                form.fields["player4"].disabled = True

        return form

    def form_valid(self, form):
        """Validate that non-staff users are participants"""
        user = self.request.user
        is_double = (form.cleaned_data.get('is_double'))
        player1 = form.cleaned_data["player1"]
        player2 = form.cleaned_data["player2"]
        player3 = form.cleaned_data["player3"]  # Optional
        player4 = form.cleaned_data["player4"]  # Optional

        # Validation for singles matches
        if not is_double:
            if player3 or player4:
                messages.error(self.request, "Singles matches cannot have Player 3 or Player 4!")
                return self.form_invalid(form)

        # Validation for doubles matches
        if is_double:
            if not player3 or not player4:
                messages.error(self.request, "Doubles matches require all 4 players!")
                return self.form_invalid(form)

            # Ensure all 4 players are unique
            players = [player1, player2, player3, player4]
            if len(set(players)) != 4:
                messages.error(self.request, "All players must be different!")
                return self.form_invalid(form)

        # Ensure player1 != player2
        if player1 == player2:
            messages.error(self.request, "Player 1 and Player 2 must be different!")
            return self.form_invalid(form)

        # Non-staff users must be participants
        if not user.is_staff:
            try:
                user_player = user.player

                # Ensure user is one of the players
                if user_player not in [player1, player2, player3, player4]:
                    messages.error(
                        self.request, "You can only create matches you participate in!"
                    )
                    return self.form_invalid(form)

                # Force user to be player1 (prevent tampering)
                if player1 != user_player:
                    messages.error(
                        self.request, "You must be Player 1 in matches you create!"
                    )
                    return self.form_invalid(form)

            except Player.DoesNotExist:
                messages.error(
                    self.request, "You must have a player profile to create matches."
                )
                return self.form_invalid(form)

        # Sides are written after the match exists (they need its pk).
        if is_double:
            self._sides = ([player1, player2], [player3, player4])
        else:
            self._sides = ([player1], [player2])

        form.instance.is_double = is_double

        # "Score live" branch — KAN-7. Doubles deferred (KAN-13).
        self._start_live = bool(self.request.POST.get("start_live")) and not is_double
        if self._start_live:
            try:
                user_player = user.player
            except (AttributeError, Player.DoesNotExist):
                messages.error(self.request, "You need a player profile to score live.")
                return self.form_invalid(form)
            messages.success(self.request, "Live match started — tap a side to score.")
            # The same service the join flow uses (KAN-152): it saves the
            # match, its sides and its host LiveScorer row together.
            self.object = create_live_match(
                form.save(commit=False),
                scorekeeper=user_player,
                side1_players=self._sides[0],
                side2_players=self._sides[1],
            )
            form.save_m2m()
            return HttpResponseRedirect(self.get_success_url())
        else:
            messages.success(self.request, "Match created successfully!")

        response = super().form_valid(form)
        set_match_sides(self.object, *self._sides)
        self.object.save()  # recompute now that the sides exist
        return response


class MatchUpdateView(LoginRequiredMixin, UpdateView):
    """View to update an existing match"""

    model = Match
    template_name = "pingpong/match_form.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not obj.user_can_edit(self.request.user):
            raise PermissionDenied
        return obj

    def get_form_class(self):
        # If match has a winner, only allow editing location and notes
        if self.object.winner_side:
            return MatchEditForm
        return MatchForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["locations"] = Location.objects.all()
        context["is_complete"] = bool(self.object.winner_side)
        return context

    def get_success_url(self):
        return reverse_lazy("pingpong:match_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        if self.object.winner_side:
            messages.success(self.request, "Match details updated successfully!")
        else:
            messages.success(self.request, "Match updated successfully!")
        return super().form_valid(form)


def is_htmx(request):
    """True when htmx issued this request, so only a fragment is wanted."""
    return request.headers.get("HX-Request") == "true"


class HowRatingsWorkView(LoginRequiredMixin, TemplateView):
    """The plain-language explainer for Glicko-2 ratings (KAN-71)."""

    template_name = "pingpong/how_ratings_work.html"


class LeaderboardView(LoginRequiredMixin, TemplateView):
    """The seasons table: one club, one season or all-time, one format."""

    template_name = "pingpong/leaderboard.html"

    def get_template_names(self):
        # htmx asks for just the results block; a normal load gets the page.
        # Both render the same partial, so they cannot drift.
        if is_htmx(self.request):
            return ["pingpong/_leaderboard_season_results.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self._season_context())
        return context

    def _season_context(self):
        """Resolve the scope from GET and build the table for it.

        Resolves the scope through `LeaderboardScopeForm`, so a bad or foreign
        season id falls back to the default (the active club's current season,
        else all-time) rather than reaching the ORM. Both cache paths then go
        through `_presentation_context`.
        """
        club = get_current_club()
        seasons = ratings.club_seasons(club)
        current = ratings.current_season(seasons)
        defaults = {
            "season": str(current.pk) if current else ratings.ALL_TIME,
            "format": rating_pipeline.SINGLES,
            "top_x": DEFAULT_TOP_X,
        }
        form_kwargs = {"seasons": seasons, "current": current, "all_time_value": ratings.ALL_TIME}
        probe = LeaderboardScopeForm(self.request.GET, **form_kwargs)
        probe.is_valid()
        chosen = {
            name: probe.cleaned_data.get(name) or default
            for name, default in defaults.items()
        }
        form = LeaderboardScopeForm(chosen, **form_kwargs)
        form.is_valid()

        season = next((s for s in seasons if str(s.pk) == chosen["season"]), None)
        scope = rating_pipeline.RatingScope(season, chosen["format"])
        top_x = chosen["top_x"]

        # Day one of a season: with fewer than 2 rated players the default
        # (current-season) table would be empty, so the *default* scope shows
        # all-time instead. An explicit choice is never overridden.
        fell_back = False
        if (
            not self.request.GET.get("season")
            and season is not None
            and ratings.season_table_is_empty(scope)
        ):
            scope = rating_pipeline.RatingScope(None, chosen["format"])
            chosen = {**chosen, "season": ratings.ALL_TIME}
            form = LeaderboardScopeForm(chosen, **form_kwargs)
            form.is_valid()
            fell_back = True

        # The club namespace and generation live in the key (cache_utils).
        cache_key = cache_utils.leaderboard_key(scope, top_x)
        payload = cache.get(cache_key)
        if payload is None:
            rows, unrated = ratings.leaderboard(scope, int(top_x))
            payload = {"rows": rows, "unrated": unrated}
            cache.set(cache_key, payload, 600)

        context = self._presentation_context(
            payload["rows"], top_x, rating_scope=scope, unrated=payload["unrated"],
        )
        context.update({
            "scope_form": form,
            "is_default_scope": chosen == defaults or fell_back,
            "current_season": current,
            "fell_back_to_all_time": fell_back,
        })
        return context

    @staticmethod
    def _presentation_context(player_stats, top_x, *, rating_scope, unrated=()):
        """Everything the template reads, derived once for both cache paths.

        `biggest_mover` is the editorial line at the foot of the table -- the
        sentence somebody would actually type when they share the screenshot.
        It is only interesting when somebody has in fact moved, so a week where
        nothing happened produces no line rather than "Nobody, +0".

        Each row's provisional status is derived here, from its cached RD
        inflated to today and its all-time experience, so the verdict is never
        cached.
        """
        movers = [row for row in player_stats if row.get("movement")]
        biggest_mover = (
            max(movers, key=lambda row: abs(row["movement"])) if movers else None
        )

        today = timezone.now().date()
        for row in player_stats:
            shown = ratings.display_rd(row, today)
            if shown is not None:
                # The verdict uses the rounded RD: the figure shown as the ±.
                row["rd"] = round(shown)
            row["status"] = ratings.rating_status(
                True, experience=row["experience"],
                rd=row["rd"] if shown is not None else None,
            )
            row["reason"] = row["status"].reason

        return {
            "player_stats": player_stats,
            "top_x": top_x,
            "biggest_mover": biggest_mover,
            "leaderboard_week": timezone.localdate().isocalendar().week,
            "rating_scope": rating_scope,
            "scope_label": ratings.scope_label(rating_scope),
            "format_label": ratings.format_label(rating_scope.format),
            "unrated_players": list(unrated),
            "show_rating_help": bool(unrated) or any(
                row["status"] == ratings.PROVISIONAL for row in player_stats
            ),
        }


class HeadToHeadStatsView(LoginRequiredMixin, TemplateView):
    """Detailed statistics comparison between two players"""

    template_name = "pingpong/head_to_head.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player1_id = self.request.GET.get("player1")
        player2_id = self.request.GET.get("player2")

        context["all_players"] = Player.objects.all()

        if player1_id and player2_id:
            player1 = get_object_or_404(Player, pk=player1_id)
            player2 = get_object_or_404(Player, pk=player2_id)

            # Try cache first (30 minute TTL)
            h2h_cache_key = (
                f'{cache_utils.club_prefix(player1.club)}'
                f'h2h_{min(player1.pk, player2.pk)}_{max(player1.pk, player2.pk)}'
            )
            cached_h2h = cache.get(h2h_cache_key)

            if cached_h2h is not None:
                context.update(cached_h2h)
                context.update(self._prediction(player1, player2))
                # Deliberately NOT part of the cached blob: the point fold has
                # its own key and its own invalidation (cache_utils), so
                # folding it in here would give one figure two TTLs and two
                # ways to go stale.
                context.update(
                    self._point_comparison(player1, player2, cached_h2h.get("matches"))
                )
                return context

            # Check if any 2v2 matches exist between these players
            has_2v2_matches = (
                Match.objects.filter(participants__player=player1)
                .filter(participants__player=player2)
                .filter(is_double=True)
                .exists()
            )
            context['has_2v2_matches'] = has_2v2_matches

            # Get all confirmed 1v1 matches between these players
            # Use is_confirmed=True for DB-level filtering
            # A true 1v1: exactly two participants, one being each player.
            all_matches = (
                Match.objects.annotate(participant_count=Count('participants'))
                .filter(participant_count=2, is_confirmed=True)
                .filter(participants__player=player1)
                .filter(participants__player=player2)
                .prefetch_related("games", participants_prefetch())
                .order_by("date_played")
            )

            matches = [cache_side_players(m) for m in all_matches]

            # Orient each match from player1's perspective. The template read
            # match.player1_score / player2_score, which nothing ever set, so
            # the score column in the match table rendered blank.
            for match in matches:
                if match.cached_player1 == player1:
                    match.player1_score = match.side1_score
                    match.player2_score = match.side2_score
                else:
                    match.player1_score = match.side2_score
                    match.player2_score = match.side1_score
                match.player1_won = _player_won(player1, match)

            if matches:
                # Basic stats (matches is now a list, not QuerySet)
                total_matches = len(matches)
                player1_match_wins = len(
                    [m for m in matches if _player_won(player1, m)]
                )
                player2_match_wins = len(
                    [m for m in matches if _player_won(player2, m)]
                )

                # Game-level analysis
                all_games = []
                point_differences = []  # For the chart like yours
                player1_game_wins = 0
                player2_game_wins = 0
                close_games = 0  # Decided by 2 points or less
                player1_dominant = 0  # Won by 5+ points
                player2_dominant = 0

                for match in matches:
                    games = match.games.all()
                    for game in games:
                        # Determine scores based on who was player1 in the match
                        if match.cached_player1 == player1:
                            p1_score = game.team1_score
                            p2_score = game.team2_score
                        else:
                            p1_score = game.team2_score
                            p2_score = game.team1_score

                        diff = p1_score - p2_score
                        point_differences.append(
                            {
                                "game_number": len(all_games) + 1,
                                "difference": diff,
                                "match_date": match.date_played,
                                "p1_score": p1_score,
                                "p2_score": p2_score,
                            }
                        )

                        all_games.append(
                            {
                                "p1_score": p1_score,
                                "p2_score": p2_score,
                                "difference": abs(diff),
                                "winner": player1 if diff > 0 else player2,
                            }
                        )

                        # Count wins
                        if diff > 0:
                            player1_game_wins += 1
                        else:
                            player2_game_wins += 1

                        # Close games
                        if abs(diff) <= 2:
                            close_games += 1

                        # Dominant games
                        if diff >= 5:
                            player1_dominant += 1
                        elif diff <= -5:
                            player2_dominant += 1

                total_games = len(all_games)

                # Calculate averages
                avg_point_diff = (
                    sum(g["difference"] for g in all_games) / total_games
                    if total_games > 0
                    else 0
                )

                # Max margins
                player1_max_margin = max(
                    [g["difference"] for g in all_games if g["winner"] == player1],
                    default=0,
                )
                player2_max_margin = max(
                    [g["difference"] for g in all_games if g["winner"] == player2],
                    default=0,
                )

                # Average scores
                avg_p1_score = (
                    sum(g["p1_score"] for g in all_games) / total_games
                    if total_games > 0
                    else 0
                )
                avg_p2_score = (
                    sum(g["p2_score"] for g in all_games) / total_games
                    if total_games > 0
                    else 0
                )

                # Recent form (last 5 matches) - matches is already ordered by date_played
                recent_matches = list(reversed(matches[-5:]))  # Get last 5 and reverse for desc order
                player1_recent_wins = sum(
                    1 for m in recent_matches if _player_won(player1, m)
                )
                player2_recent_wins = sum(
                    1 for m in recent_matches if _player_won(player2, m)
                )

                # Match margins (for average margin per match chart)
                match_margins = []
                for match in matches:
                    if _player_won(player1, match):
                        margin = (
                            match.team1_score - match.team2_score
                            if match.cached_player1 == player1
                            else match.team2_score - match.team1_score
                        )
                    elif _player_won(player2, match):
                        margin = -(
                            match.team1_score - match.team2_score
                            if match.cached_player1 == player1
                            else match.team2_score - match.team1_score
                        )
                    else:
                        margin = 0

                    match_margins.append(
                        {
                            "match_number": len(match_margins) + 1,
                            "margin": margin,
                            "date": match.date_played,
                        }
                    )

                # Calculate average margin per match
                cumulative_avg = []
                running_total = 0
                for i, m in enumerate(match_margins, 1):
                    running_total += m["margin"]
                    cumulative_avg.append(running_total / i)

                h2h_data = {
                    "player1": player1,
                    "player2": player2,
                    "has_data": True,
                    "has_2v2_matches": has_2v2_matches,
                    "total_matches": total_matches,
                    "total_games": total_games,
                    "player1_match_wins": player1_match_wins,
                    "player2_match_wins": player2_match_wins,
                    "player1_game_wins": player1_game_wins,
                    "player2_game_wins": player2_game_wins,
                    "player1_match_win_rate": (
                        player1_match_wins / total_matches * 100
                    )
                    if total_matches > 0
                    else 0,
                    "player2_match_win_rate": (
                        player2_match_wins / total_matches * 100
                    )
                    if total_matches > 0
                    else 0,
                    "player1_game_win_rate": (player1_game_wins / total_games * 100)
                    if total_games > 0
                    else 0,
                    "player2_game_win_rate": (player2_game_wins / total_games * 100)
                    if total_games > 0
                    else 0,
                    "close_games": close_games,
                    "player1_dominant": player1_dominant,
                    "player2_dominant": player2_dominant,
                    "avg_point_diff": avg_point_diff,
                    "player1_max_margin": player1_max_margin,
                    "player2_max_margin": player2_max_margin,
                    "avg_p1_score": avg_p1_score,
                    "avg_p2_score": avg_p2_score,
                    "player1_recent_wins": player1_recent_wins,
                    "player2_recent_wins": player2_recent_wins,
                    "recent_total": min(5, total_matches),
                    "point_differences_json": json.dumps(
                        point_differences, cls=DjangoJSONEncoder
                    ),
                    "match_margins_json": json.dumps(
                        match_margins, cls=DjangoJSONEncoder
                    ),
                    "cumulative_avg_json": json.dumps(cumulative_avg),
                    "matches": list(reversed(matches)),
                }

                # Cache for 30 minutes. The prediction is deliberately NOT in
                # the blob: it depends on the viewer's rating system.
                cache.set(h2h_cache_key, h2h_data, 1800)
                context.update(h2h_data)
                context.update(self._prediction(player1, player2))
                context.update(
                    self._point_comparison(player1, player2, h2h_data["matches"])
                )
            else:
                h2h_data = {
                    "player1": player1,
                    "player2": player2,
                    "has_data": False,
                    "has_2v2_matches": has_2v2_matches,
                }
                cache.set(h2h_cache_key, h2h_data, 1800)
                context.update(h2h_data)
                context.update(self._prediction(player1, player2))
                context.update(self._point_comparison(player1, player2, []))

        return context

    def _prediction(self, player1, player2):
        """Head-to-head win chance (not cached).

        Singles in the current season (else all-time), computed per request.
        """
        scope = ratings.scope_for(player1.club_id, timezone.now(), False)
        p1_pct, p2_pct = ratings.win_probability([player1], [player2], scope=scope)
        prediction = {
            "player1_win_pct": p1_pct,
            "player2_win_pct": p2_pct,
        }
        # The two readings the bar was computed from, for display.
        readings = ratings.glicko_readings([player1, player2], scope=scope)
        prediction["player1_rating"], prediction["player1_rd"] = readings[player1.pk]
        prediction["player2_rating"], prediction["player2_rd"] = readings[player2.pk]
        return prediction

    @staticmethod
    def _point_comparison(player1, player2, shared_matches):
        """Both players' point records over the matches they shared (S2.7).

        **Restricted at the query level, never by subtracting a career.**
        Folding each player's whole history and taking a difference would put
        their global rate into a comparison whose entire purpose is to say how
        they do against *this* opponent -- the one number this screen exists
        to deny. `point_rollups.head_to_head_point_stats` filters on
        `match_id__in`.

        The match set is the one the rest of the page already uses (confirmed
        singles between the two), so the two halves of the screen cannot
        describe different sets of matches.

        Absent when they have no shared live-scored matches: the existing
        head-to-head content is then unchanged.
        """
        # A player compared with themselves would render two identical
        # columns, which says nothing and reads as a bug. The rest of this
        # page has the same quirk and is left as it is -- widening the fix is
        # not this ticket -- but a new section need not repeat it.
        if player1.pk == player2.pk:
            return {"has_point_comparison": False}

        match_ids = [m.pk for m in (shared_matches or [])]
        if not match_ids:
            return {"has_point_comparison": False}

        stats = point_rollups.head_to_head_point_stats(player1, player2, match_ids)
        one, two = stats[player1.pk], stats[player2.pk]
        if not one.has_sample and not two.has_sample:
            return {"has_point_comparison": False}

        return {
            "has_point_comparison": True,
            "point_h2h_player1": one,
            "point_h2h_player2": two,
            "point_h2h_confident": (
                one.serve.is_confident() and two.serve.is_confident()
            ),
            "point_min_sample": point_stats.MIN_SAMPLE_POINTS,
        }


@method_decorator(
    ratelimit(key='ip', rate='5/h', method='POST', block=True),
    name='post'
)
class PlayerRegistrationView(CreateView):
    """View that creates User + Player

    Rate limited to 5 registrations per hour per IP to prevent spam.
    """

    form_class = PlayerRegistrationForm
    template_name = "registration/signup.html"
    success_url = reverse_lazy("pingpong:dashboard")

    def dispatch(self, request, *args, **kwargs):
        # Redirect if already logged in
        if request.user.is_authenticated:
            return redirect("pingpong:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        """Log the user in after successful registration"""
        response = super().form_valid(form)
        user = self.object  # type: ignore

        # If they arrived via an invite link, put them in the club now. The
        # invite flow hooks into this view rather than forking it -- a second
        # registration path would drift from this one, which already creates
        # the Player, sends verification and logs the user in.
        invited_club = consume_pending_invite(self.request, user=user)
        verification_url = self.request.build_absolute_uri(
            reverse_lazy(
                "pingpong:email_verify", args=[user.profile.email_verification_token]
            )
        )

        send_mail(
            subject="Verify your email address",
            message=f"Welcome {user.username}! Click here to verify your email: {verification_url}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )

        return render(
            self.request,
            "registration/verify_email_sent.html",
            {
                "email": user.email,
                "username": user.username,
                "invited_club": invited_club,
            },
        )

    def form_invalid(self, form):
        """Return generic error to prevent account enumeration"""
        messages.error(self.request, "Registration failed. Please check the form and try again.")
        return super().form_invalid(form)


@login_required
def match_confirm(request, pk):
    """Allow a player to confirm a match"""
    match = get_object_or_404(Match, pk=pk)

    try:
        user_player = Player.objects.get(user=request.user)

        # Verify the player belongs to one of the two teams
        if not match.participants.filter(player=user_player).exists():
            messages.error(request, "You are not a player in this match.")
            return redirect("pingpong:match_detail", pk=pk)

        # Create confirmations (does not duplicate existing ones)
        MatchConfirmation.objects.get_or_create(
            match=match,
            player=user_player
        )

        messages.success(request, "You have confirmed this match!")
    except Player.DoesNotExist:
        messages.error(request, "You must have a player profile to confirm matches.")

    return redirect("pingpong:match_detail", pk=pk)


class EmailVerifyView(View):
    """Verify email with token"""

    def get(self, request, token):
        try:
            profile = UserProfile.objects.get(email_verification_token=token)

            # Check if already verified
            if profile.email_verified:
                messages.info(request, "Your email is already verified!")
                # If already logged in, go to dashboard
                if request.user.is_authenticated:
                    return redirect("pingpong:dashboard")
                # Otherwise go to login
                return redirect("pingpong:login")

            # Verify the email
            if profile.verify_email(token):
                # Log the user in automatically after verification
                login(
                    request,
                    profile.user,
                    backend="django.contrib.auth.backends.ModelBackend",
                )
                messages.success(
                    request,
                    f"Welcome, {profile.user.username}! Your email has been verified.",
                )
                return redirect("pingpong:dashboard")
            else:
                messages.error(request, "Invalid or expired verification token.")
                return redirect("pingpong:login")

        except UserProfile.DoesNotExist:
            messages.error(request, "Invalid verification link.")
            return redirect("pingpong:login")


@method_decorator(
    ratelimit(key='user', rate='3/h', method='POST', block=True),
    name='post'
)
class EmailResendVerificationView(LoginRequiredMixin, View):
    """Resend verification email

    Rate limited to 3 resends per hour per user to prevent abuse.
    """

    def post(self, request):
        profile = request.user.profile
        user = request.user

        if profile.email_verified:
            messages.info(request, "Your email is already verified.")
        else:
            # Generate new token
            token = profile.create_verification_token()
            profile.save()

            verification_url = request.build_absolute_uri(
                f"/pingpong/verify-email/{token}/"
            )

            try:
                send_mail(
                    subject="Verify your email address",
                    message=f"Welcome {user.username}! Click here to verify your email: {verification_url}",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=False,
                )
                messages.success(
                    request,
                    f"Verification email sent! Check your inbox at {request.user.email}",
                )
            except Exception as e:
                logger.error(f"Failed to send verification email to {user.email}: {e}")
                messages.error(request, "Failed to send verification email. Please try again later.")

        # Redirect to player profile if exists, otherwise dashboard
        if hasattr(request.user, "player"):
            return redirect("pingpong:player_detail", pk=request.user.player.pk)
        return redirect("pingpong:dashboard")


class ScheduledMatchCreateView(LoginRequiredMixin, CreateView):
    """View to schedule a future match"""

    model = ScheduledMatch
    form_class = ScheduledMatchForm
    template_name = "pingpong/scheduled_match_form.html"
    success_url = reverse_lazy("pingpong:calendar")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["locations"] = Location.objects.all()
        context["is_staff"] = self.request.user.is_staff
        try:
            context["user_player"] = self.request.user.player
        except AttributeError:
            context["user_player"] = None
        return context

    def get_form(self, form_class=None):
        """Customize form based on user permissions"""
        form = super().get_form(form_class)
        user = self.request.user

        if not user.is_staff:
            try:
                user_player = user.player

                # Pre-select and lock user as player1
                form.fields["player1"].initial = user_player
                form.fields["player1"].disabled = True
                append_widget_class(
                    form.fields["player1"], "bg-muted cursor-not-allowed"
                )
                form.fields["player1"].help_text = "You are automatically set as Player 1"

                # Limit player2 choices to exclude the user
                form.fields["player2"].queryset = Player.objects.exclude(
                    pk=user_player.pk
                )

            except Player.DoesNotExist:
                messages.error(
                    self.request,
                    "You must have a player profile to schedule matches. Please contact an administrator.",
                )
                form.fields["player1"].disabled = True
                form.fields["player2"].disabled = True

        return form

    def form_valid(self, form):
        """Save the scheduled match and send notifications"""
        user = self.request.user
        player1 = form.cleaned_data["player1"]
        player2 = form.cleaned_data["player2"]

        # Ensure player1 != player2
        if player1 == player2:
            messages.error(self.request, "Player 1 and Player 2 must be different!")
            return self.form_invalid(form)

        # Non-staff users must be participants
        if not user.is_staff:
            try:
                user_player = user.player

                # Ensure user is one of the players
                if user_player not in [player1, player2]:
                    messages.error(
                        self.request, "You can only create matches you participate in!"
                    )
                    return self.form_invalid(form)

                # Force user to be player1 (prevent tampering)
                if player1 != user_player:
                    messages.error(
                        self.request, "You must be Player 1 in matches you create!"
                    )
                    return self.form_invalid(form)

                form.instance.created_by = user_player

            except Player.DoesNotExist:
                messages.error(
                    self.request, "You must have a player profile to schedule matches."
                )
                return self.form_invalid(form)

        # Save first, then attach the sides (they need the pk).
        self.object = form.save()
        scheduled_match = self.object
        set_scheduled_match_sides(scheduled_match, [player1], [player2])

        # Push first, email only whoever push did not reach. Same rule as the
        # match-confirmation path in signals.py.
        pushed = notifications.notify_scheduled_match(
            scheduled_match, [player1, player2]
        )
        for player in (player1, player2):
            if player.pk not in pushed:
                send_scheduled_match_email(scheduled_match, player)

        # Mark notification as sent
        scheduled_match.notification_sent = True
        scheduled_match.save()

        messages.success(
            self.request,
            f"Match scheduled for {scheduled_match.scheduled_date.strftime('%B %d, %Y')}! Notifications sent to both players.",
        )

        # Redirect to calendar showing the month of the scheduled match
        return redirect(
            f"{reverse_lazy('pingpong:calendar')}?year={scheduled_match.scheduled_date.year}&month={scheduled_match.scheduled_date.month}"
        )


class CalendarView(LoginRequiredMixin, TemplateView):
    """Display calendar view of scheduled and past matches"""

    template_name = "pingpong/calendar.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.utils import timezone
        import calendar
        from datetime import date

        # Get month/year from query params or use current
        today = timezone.now().date()
        year = int(self.request.GET.get("year", today.year))
        month = int(self.request.GET.get("month", today.month))

        # Create current date for the selected month
        current_date = date(year, month, 1)

        # Calculate previous/next month as date objects (for template .month/.year access)
        if month == 1:
            prev_month = date(year - 1, 12, 1)
        else:
            prev_month = date(year, month - 1, 1)

        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)

        # Get user's player
        try:
            user_player = self.request.user.player
        except AttributeError:
            user_player = None

        # Get scheduled matches for this month (with match data for filtering)
        scheduled_matches = ScheduledMatch.objects.filter(
            scheduled_date__year=year,
            scheduled_date__month=month,
        ).select_related('match').prefetch_related(
            'match__confirmations'
        ).order_by("scheduled_date", "scheduled_time")

        # Get completed matches for this month
        # Exclude matches with related_name to scheduled match for prefetching
        completed_matches = Match.objects.filter(
            date_played__year=year,
            date_played__month=month,
        ).select_related('scheduled_from').order_by("date_played")

        # Organize matches by day
        matches_by_day = {}
        for sm in scheduled_matches:
            # Skip fully confirmed scheduled matches
            if sm.match and sm.match.match_confirmed:
                continue

            day = sm.scheduled_date.day
            if day not in matches_by_day:
                matches_by_day[day] = []
            sm.is_scheduled = True
            matches_by_day[day].append(sm)

        for m in completed_matches:
            # Skip matches that came from scheduled matches and aren't fully confirmed
            # (they're already shown as scheduled matches above)
            if not m.match_confirmed:
                continue

            day = m.date_played.day
            if day not in matches_by_day:
                matches_by_day[day] = []
            m.is_scheduled = False
            matches_by_day[day].append(m)

        # Build calendar weeks structure for the template
        cal = calendar.Calendar(firstweekday=6)  # Sunday first
        calendar_weeks = []
        for week in cal.monthdatescalendar(year, month):
            week_data = []
            for day_date in week:
                day_matches = matches_by_day.get(day_date.day, []) if day_date.month == month else []
                week_data.append({
                    'day': day_date.day,
                    'date': day_date,
                    'is_other_month': day_date.month != month,
                    'is_today': day_date == today,
                    'matches': day_matches,
                })
            calendar_weeks.append(week_data)

        # Get upcoming scheduled matches (all future)
        upcoming_matches = ScheduledMatch.objects.filter(
            scheduled_date__gte=today
        ).prefetch_related(
            participants_prefetch(scheduled=True)
        ).order_by("scheduled_date", "scheduled_time")[:5]

        # Attach win probability to upcoming matches
        for sm in upcoming_matches:
            cache_side_players(sm)
            t1_pct, t2_pct = ratings.win_probability(
                sm.cached_team1_players, sm.cached_team2_players,
                scope=ratings.scope_for(
                    sm.club_id, sm.scheduled_date,
                    max(len(sm.cached_team1_players), len(sm.cached_team2_players)) > 1,
                ),
                day=sm.scheduled_date,
            )
            sm.team1_win_pct = t1_pct
            sm.team2_win_pct = t2_pct

        context.update(
            {
                "current_date": current_date,
                "prev_month": prev_month,
                "next_month": next_month,
                "today": today,
                "calendar_weeks": calendar_weeks,
                "upcoming_matches": upcoming_matches,
                "user_player": user_player,
            }
        )

        return context


class ScheduledMatchDetailView(LoginRequiredMixin, DetailView):
    """View to display scheduled match details and conversion status"""

    model = ScheduledMatch
    template_name = "pingpong/scheduled_match_detail.html"
    context_object_name = "scheduled_match"

    def get_queryset(self):
        """Filter by user permissions"""
        return ScheduledMatch.objects.all()

    def get_object(self, queryset=None):
        """Check permissions and return object"""
        from django.core.exceptions import PermissionDenied
        obj = super().get_object(queryset)

        # Check if user can view this scheduled match
        if not obj.user_can_view(self.request.user):
            raise PermissionDenied("You don't have permission to view this scheduled match.")

        return obj

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scheduled_match = self.object
        user = self.request.user

        # Add conversion status
        context['is_converted'] = scheduled_match.is_converted
        context['is_fully_confirmed'] = scheduled_match.is_fully_confirmed

        # Check if user can convert (participant or staff)
        can_convert = scheduled_match.user_can_view(user)
        context['can_convert'] = can_convert

        # Win probability
        t1_pct, t2_pct = ratings.win_probability(
            scheduled_match.side1_players, scheduled_match.side2_players,
            scope=ratings.scope_for(
                scheduled_match.club_id, scheduled_match.scheduled_date,
                max(len(scheduled_match.side1_players), len(scheduled_match.side2_players)) > 1,
            ),
            day=scheduled_match.scheduled_date,
        )
        context['team1_win_pct'] = t1_pct
        context['team2_win_pct'] = t2_pct

        return context


class ScheduledMatchConvertView(LoginRequiredMixin, CreateView):
    """View to convert scheduled match to played match"""

    model = Match
    form_class = MatchConvertForm
    template_name = "pingpong/scheduled_match_convert.html"

    def dispatch(self, request, *args, **kwargs):
        """Check if already converted and permissions"""
        # Check authentication first (before fetching object)
        if not request.user.is_authenticated:
            # Let LoginRequiredMixin handle the redirect
            return super().dispatch(request, *args, **kwargs)

        # User is authenticated, safe to fetch object
        # _base_manager bypasses the *user* filter so a championship organiser
        # can convert a match they are not playing in. Scoped, so it does not
        # also bypass the club filter.
        try:
            self.scheduled_match = scope_to_active_club(
                ScheduledMatch._base_manager.all()
            ).get(pk=self.kwargs['scheduled_match_pk'])
        except ScheduledMatch.DoesNotExist:
            from django.http import Http404
            raise Http404("Scheduled match not found")

        # Check permissions
        if not self.scheduled_match.user_can_view(request.user):
            messages.error(request, "You don't have permission to convert this scheduled match.")
            return redirect("pingpong:calendar")

        # Check if already converted
        if self.scheduled_match.is_converted:
            messages.info(request, "This scheduled match has already been converted to a played match.")
            return redirect("pingpong:match_detail", pk=self.scheduled_match.match.pk)

        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        """Pass scheduled_match and user to form"""
        kwargs = super().get_form_kwargs()
        kwargs['scheduled_match'] = self.scheduled_match
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['scheduled_match'] = self.scheduled_match
        context['locations'] = Location.objects.all()
        return context

    def get_success_url(self):
        """Redirect to match detail page"""
        return reverse_lazy("pingpong:match_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        """Create match, link to scheduled match, handle teams"""
        user = self.request.user
        is_double = form.cleaned_data.get('is_double')
        player1 = form.cleaned_data["player1"]
        player2 = form.cleaned_data["player2"]
        player3 = form.cleaned_data.get("player3")
        player4 = form.cleaned_data.get("player4")

        # Validation for singles matches
        if not is_double:
            if player3 or player4:
                messages.error(self.request, "Singles matches cannot have Player 3 or Player 4!")
                return self.form_invalid(form)

        # Validation for doubles matches
        if is_double:
            if not player3 or not player4:
                messages.error(self.request, "Doubles matches require all 4 players!")
                return self.form_invalid(form)

            # Ensure all 4 players are unique
            players = [player1, player2, player3, player4]
            if len(set(players)) != 4:
                messages.error(self.request, "All players must be different!")
                return self.form_invalid(form)

        # Ensure player1 != player2
        if player1 == player2:
            messages.error(self.request, "Player 1 and Player 2 must be different!")
            return self.form_invalid(form)

        # Non-staff users must be participants
        if not user.is_staff:
            try:
                user_player = user.player

                # Ensure user is one of the players
                if user_player not in [player1, player2, player3, player4]:
                    messages.error(
                        self.request, "You can only convert matches you participate in!"
                    )
                    return self.form_invalid(form)

            except Player.DoesNotExist:
                messages.error(
                    self.request, "You must have a player profile to convert matches."
                )
                return self.form_invalid(form)

        # Note the pairing differs from MatchCreateView: this form lays out
        # side 1 as player1+player3.
        if is_double:
            self._sides = ([player1, player3], [player2, player4])
        else:
            self._sides = ([player1], [player2])

        form.instance.is_double = is_double

        # Save the match, then attach its sides (they need the pk)
        response = super().form_valid(form)
        set_match_sides(self.object, *self._sides)
        self.object.save()

        # Link scheduled match to this match
        self.scheduled_match.match = self.object
        self.scheduled_match.save()

        # Propagate championship FK if this is a championship match
        if self.scheduled_match.championship:
            self.object.championship = self.scheduled_match.championship
            self.object.match_type = 'tournament'
            self.object.save(update_fields=['championship', 'match_type'])

            # Auto-transition championship from scheduled -> in_progress
            championship = self.scheduled_match.championship
            if championship.status == Championship.Status.SCHEDULED:
                championship.status = Championship.Status.IN_PROGRESS
                championship.save(update_fields=['status'])

        messages.success(
            self.request,
            "Match recorded successfully! Now add game scores to complete the match."
        )
        return response


@method_decorator(
    ratelimit(key='ip', rate='5/15m', method='POST', block=True),
    name='post'
)
class CustomLoginView(LoginView):
    """Custom login view with tailwind styling

    Rate limited to 5 login attempts per 15 minutes per IP to prevent brute force.
    """

    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_success_url(self):
        # A safe `next` wins (LoginView.get_redirect_url checks the host): the
        # scoreboard's "Sign in again" link must land back on the scoreboard,
        # where the kept outbox then flushes (KAN-81). Otherwise Today.
        return self.get_redirect_url() or reverse_lazy("pingpong:dashboard")

    def form_valid(self, form):
        user = form.get_user()
        # Check if email is verified
        if hasattr(user, "profile") and not user.profile.email_verified:
            messages.warning(
                self.request,
                "Please verify your email before logging in. Check your inbox!",
            )
            return redirect("pingpong:login")

        messages.success(self.request, f"Welcome back, {user.username}!")
        return super().form_valid(form)


class PasskeyManagementView(LoginRequiredMixin, View):
    """View for users to manage their passkeys"""
    template_name = "pingpong/passkey_management.html"

    def get(self, request):
        if WebAuthnCredential is None:
            messages.error(request, "Passkey functionality is not available.")
            return redirect("pingpong:dashboard")

        credentials = WebAuthnCredential.objects.filter(user=request.user)
        return render(request, self.template_name, {
            'credentials': credentials
        })

    def post(self, request):
        if WebAuthnCredential is None:
            messages.error(request, "Passkey functionality is not available.")
            return redirect("pingpong:dashboard")

        credential_id = request.POST.get('credential_id')
        credential = get_object_or_404(
            WebAuthnCredential,
            pk=credential_id,
            user=request.user
        )

        # Send notification email before deleting
        device_name = credential.name
        send_passkey_deleted_email(request.user, device_name)

        credential.delete()
        messages.success(request, f"Passkey '{device_name}' deleted")
        return redirect('pingpong:passkey_management')

class ChampionshipListView(LoginRequiredMixin, ListView):
    """View to list all championships"""

    template_name = "pingpong/championship_list.html"
    context_object_name = "championships"
    model = Championship
    paginate_by = 10

    def get_queryset(self):
        user = self.request.user

        # Get filter parameters
        status_filter = self.request.GET.get('status', 'all')
        participation_filter = self.request.GET.get('participation', 'all')

        queryset = Championship.objects.select_related(
            'created_by', 'location'
        ).prefetch_related(
            'entries__members__player'
        )

        # Filter by status (supports tab-based filtering)
        if status_filter == 'upcoming':
            queryset = queryset.filter(status__in=[Championship.Status.REGISTRATION, Championship.Status.SCHEDULED])
        elif status_filter == 'past':
            queryset = queryset.filter(status__in=[Championship.Status.COMPLETED, Championship.Status.CANCELLED])
        elif status_filter != 'all':
            queryset = queryset.filter(status=status_filter)

        # Filter by participation
        if participation_filter == 'my_championships':
            # Championships where user is a participant
            try:
                player = user.player
                queryset = queryset.filter(entry_members__player=player)
            except (AttributeError, Player.DoesNotExist):
                queryset = queryset.none()
        elif participation_filter == 'public':
            queryset = queryset.filter(is_public=True)
        # else: default - manager already handles visibility filtering

        return queryset.distinct().order_by('-start_date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status_filter'] = self.request.GET.get('status', 'all')
        context['participation_filter'] = self.request.GET.get('participation', 'all')
        return context


class ChampionshipDetailView(LoginRequiredMixin, DetailView):
    """View to show championship details"""

    template_name = "pingpong/championship_detail.html"
    context_object_name = "championship"
    model = Championship

    def get_queryset(self):
        # Manager already handles visibility filtering
        return Championship.objects.select_related(
            'created_by', 'location'
        ).prefetch_related(
            'entries__members__player'
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        championship = self.object

        # Get standings
        context['standings'] = championship.get_standings()
        context['can_edit'] = championship.user_can_edit(self.request.user)

        # Get scheduled matches - use all_objects to bypass manager filtering
        # (championship participants should see all championship matches)
        scheduled_matches = ScheduledMatch.all_objects.filter(
            championship=championship
        ).select_related(
            'location', 'match'
        ).prefetch_related(
            participants_prefetch(scheduled=True)
        ).order_by('round_number', 'scheduled_date', 'scheduled_time')
        context['scheduled_matches'] = scheduled_matches

        # Group scheduled matches by round number for display
        grouped = {}
        for sm in scheduled_matches:
            round_num = sm.round_number or 0
            if round_num not in grouped:
                grouped[round_num] = []
            grouped[round_num].append(sm)
        context['grouped_scheduled_matches'] = sorted(grouped.items())

        # Progress tracking
        total_matches = scheduled_matches.count()
        converted_matches = scheduled_matches.filter(match__isnull=False).count()
        context['total_scheduled'] = total_matches
        context['converted_count'] = converted_matches
        context['progress_pct'] = int(converted_matches / total_matches * 100) if total_matches > 0 else 0

        # Get completed matches - use all_objects to bypass manager filtering
        completed_matches = Match.all_objects.filter(
            championship=championship
        ).select_related(
            'location'
        ).prefetch_related(
            participants_prefetch(), 'games', 'confirmations'
        ).order_by('-date_played')
        context['completed_matches'] = completed_matches
        context['completed_count'] = completed_matches.count()

        # Pass participants count to avoid multiple COUNT queries in template
        context['participants_count'] = championship.entries.count()

        # Build results matrix for round-robin display
        # Use winner_side__isnull=False instead of is_confirmed=True because
        # championship matches may have winners but not yet be confirmed
        confirmed_champ_matches = Match.all_objects.filter(
            championship=championship, winner_side__isnull=False
        )

        matrix = {}
        for match in confirmed_champ_matches:
            matrix[(match.side1_entry_id, match.side2_entry_id)] = {
                'score': f'{match.team1_score_cache}-{match.team2_score_cache}',
                'won': match.winner_side == Side.ONE,
                'match_pk': match.pk,
            }

        standings = context.get('standings', [])
        matrix_teams = [s['entry'] for s in standings]
        matrix_rows = []
        for entry in matrix_teams:
            row = []
            for opponent in matrix_teams:
                if entry.pk == opponent.pk:
                    row.append({'self': True})
                else:
                    result = matrix.get((entry.pk, opponent.pk))
                    row.append(result if result else {'pending': True})
            matrix_rows.append({
                'team': entry,
                'entry': entry,
                'display_name': str(entry),
                'cells': row,
            })
        context['matrix_rows'] = matrix_rows
        context['matrix_teams'] = matrix_teams

        # Check if user can register
        try:
            player = self.request.user.player
            # Get user's teams that match championship type
            already_entered = championship.entry_members.filter(
                player=player
            ).exists()

            # Doubles entries need a partner, so offer the other players who
            # have not entered yet.
            available_partners = Player.objects.exclude(pk=player.pk).exclude(
                pk__in=championship.entry_members.values_list('player_id', flat=True)
            ).order_by('name')

            context['can_register'] = (
                    championship.is_registration_open
                    and not championship.is_full
                    and not already_entered
            )
            context['needs_partner'] = (
                championship.championship_type
                == Championship.ChampionshipType.DOUBLES
            )
            context['available_partners'] = available_partners
            context['user_teams'] = []
        except (AttributeError, Player.DoesNotExist):
            context['can_register'] = False
            context['needs_partner'] = False
            context['available_partners'] = []
            context['user_teams'] = []

        return context


class MatchValidateView(LoginRequiredMixin, View):
    """Validate a partially-filled match form without saving anything.

    match_form.html used to re-implement MatchForm.clean() in JavaScript:
    it rebuilt every player dropdown on each change to remove already-picked
    players, and toggled `required` on player3/player4 by hand. Two copies of
    the same rules, and only the Python one actually decides whether a match
    can be created.
    """

    def post(self, request, *args, **kwargs):
        form = MatchForm(request.POST)
        form.is_valid()

        # Drop only Django's own "this field is required" errors: the user is
        # still filling the form in, and nagging about fields they have not
        # reached yet is noise. Match on the error *code*, not the message --
        # "Four players are required for a doubles match!" is a rule
        # violation that happens to contain the word "required".
        errors = [
            error.message % (error.params or {}) if error.params else error.message
            for field_errors in form.errors.as_data().values()
            for error in field_errors
            if error.code != "required"
        ]
        return render(
            request, "pingpong/_form_errors.html", {"errors": errors}
        )


class ChampionshipParticipantsFragmentView(LoginRequiredMixin, TemplateView):
    """The participant picker, re-rendered for a given championship type.

    The create form used to respond to a type change by rewriting the URL and
    reloading the whole page, discarding anything already typed in. htmx
    swaps just this fragment instead.
    """

    template_name = "pingpong/_championship_participants.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        championship_type = self.request.GET.get(
            "championship_type", Championship.ChampionshipType.SINGLES
        )
        context["form"] = ChampionshipCreateForm(championship_type=championship_type)
        # Private championships are the ones that pick participants up front;
        # is_public arrives via hx-include so the swap keeps the current
        # visibility rather than resetting it.
        context["show_participants"] = self.request.GET.get("is_public") != "on"
        return context


class ChampionshipCreateView(LoginRequiredMixin, CreateView):
    """View to create a new championship"""

    template_name = "pingpong/championship_form.html"
    form_class = ChampionshipCreateForm
    model = Championship

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # Pass championship_type if provided in GET params
        championship_type = self.request.GET.get('type', Championship.ChampionshipType.SINGLES)
        kwargs['championship_type'] = championship_type
        return kwargs

    def form_valid(self, form):
        # Set creator
        try:
            form.instance.created_by = self.request.user.player
        except (AttributeError, Player.DoesNotExist):
            messages.error(self.request, "You need a player profile to create championships")
            return redirect('pingpong:championship_list')

        # Save championship
        response = super().form_valid(form)
        championship = self.object

        # Add participants for private championships
        if not championship.is_public:
            private_participants = form.cleaned_data.get('private_participants')
            for player in private_participants or []:
                championship.register_entry([player])
                # Change status to scheduled
                championship.status = Championship.Status.SCHEDULED
                championship.save()

                # Generate schedule
                if championship.generate_schedule():
                    messages.success(
                        self.request,
                        f"Championship '{championship.name}' created successfully! Schedule generated with {championship.scheduled_matches.count()} matches."
                    )
                else:
                    messages.warning(
                        self.request,
                        f"Championship created but schedule generation failed. Add more participants."
                    )
        else:
            messages.success(
                self.request,
                f"Championship '{championship.name}' created! Registration is now open."
            )

        return response

    def get_success_url(self):
        return reverse_lazy('pingpong:championship_detail', kwargs={'pk': self.object.pk})


class ChampionshipEditView(LoginRequiredMixin, UpdateView):
    """View to edit championship details"""

    template_name = "pingpong/championship_form.html"
    form_class = ChampionshipEditForm
    model = Championship

    def get_queryset(self):
        # Only allow editing own championships or staff
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return Championship.objects.all()

        try:
            player = user.player
            return Championship.objects.filter(created_by=player)
        except (AttributeError, Player.DoesNotExist):
            return Championship.objects.none()

    def form_valid(self, form):
        messages.success(self.request, "Championship updated successfully!")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy('pingpong:championship_detail', kwargs={'pk': self.object.pk})


class ChampionshipRegisterView(LoginRequiredMixin, View):
    """View to register a team for a championship"""

    def post(self, request, pk):
        championship = get_object_or_404(Championship, pk=pk)
        try:
            player = request.user.player
        except (AttributeError, Player.DoesNotExist):
            messages.error(request, "You need a player profile to register")
            return redirect('pingpong:championship_detail', pk=pk)

        # You always enter as yourself; doubles additionally needs a partner.
        entry_players = [player]
        if championship.championship_type == Championship.ChampionshipType.DOUBLES:
            partner_id = request.POST.get('partner') or request.POST.get('team')
            if not partner_id:
                messages.error(request, "Please choose a partner to register")
                return redirect('pingpong:championship_detail', pk=pk)
            partner = get_object_or_404(Player, pk=partner_id)
            if partner.pk == player.pk:
                messages.error(request, "You cannot partner with yourself")
                return redirect('pingpong:championship_detail', pk=pk)
            entry_players.append(partner)

        entry = championship.register_entry(entry_players)
        if entry is not None:
            messages.success(
                request,
                f"Successfully registered {entry} for {championship.name}!"
            )
        else:
            messages.error(
                request,
                "Unable to register. Championship may be full, registration "
                "closed, or one of you is already entered."
            )

        return redirect('pingpong:championship_detail', pk=pk)


class ChampionshipStartView(LoginRequiredMixin, View):
    """View to start a championship (close registration and generate schedule)"""

    def post(self, request, pk):
        championship = get_object_or_404(Championship, pk=pk)

        # Check permissions
        if not championship.user_can_edit(request.user):
            messages.error(request, "You don't have permission to start this championship")
            return redirect('pingpong:championship_detail', pk=pk)

        # Check if championship can be started
        if championship.status != Championship.Status.REGISTRATION:
            messages.error(request, "Championship is not in registration phase")
            return redirect('pingpong:championship_detail', pk=pk)

        if championship.current_participants_count < 2:
            messages.error(request, "Need at least 2 participants to start championship")
            return redirect('pingpong:championship_detail', pk=pk)

        # Generate schedule
        if championship.generate_schedule():
            championship.status = Championship.Status.SCHEDULED
            championship.save()
            messages.success(
                request,
                f"Championship started! Generated {championship.scheduled_matches.count()} matches."
            )
        else:
            messages.error(request, "Failed to generate championship schedule")

        return redirect('pingpong:championship_detail', pk=pk)


class ChampionshipUnregisterView(LoginRequiredMixin, View):
    """View to unregister from a championship"""

    def post(self, request, pk):
        championship = get_object_or_404(Championship, pk=pk)

        try:
            player = request.user.player
        except (AttributeError, Player.DoesNotExist):
            messages.error(request, "You need a player profile")
            return redirect('pingpong:championship_detail', pk=pk)

        # You can only withdraw the entry you are part of.
        entry = championship.entries.filter(members__player=player).first()
        if entry is None:
            messages.error(request, "You are not registered for this championship")
            return redirect('pingpong:championship_detail', pk=pk)

        # Check if championship allows unregistration
        if championship.status != Championship.Status.REGISTRATION:
            messages.error(request, "Cannot unregister after championship has started")
            return redirect('pingpong:championship_detail', pk=pk)

        label = str(entry)
        entry.delete()

        messages.success(
            request, f"Successfully unregistered {label} from {championship.name}"
        )

        return redirect('pingpong:championship_detail', pk=pk)


class ScheduledMatchEditView(LoginRequiredMixin, UpdateView):
    """View to edit scheduled match date/time (for championship organizers)."""

    model = ScheduledMatch
    form_class = ScheduledMatchEditForm
    template_name = "pingpong/scheduled_match_edit.html"

    def get_queryset(self):
        # all_objects bypasses the *user* filter so a championship organiser
        # can edit a match they are not playing in. Scoped to the club.
        return scope_to_active_club(ScheduledMatch.all_objects.all())

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)

        self.object = self.get_object()

        # Only allow editing championship scheduled matches by organizer/staff
        if self.object.championship:
            if not self.object.championship.user_can_edit(request.user):
                messages.error(request, "Only the championship organizer can edit scheduled matches.")
                return redirect('pingpong:championship_detail', pk=self.object.championship.pk)
        else:
            # For non-championship scheduled matches, check the match's own permissions
            if not self.object.user_can_edit(request.user):
                messages.error(request, "You don't have permission to edit this scheduled match.")
                return redirect('pingpong:scheduled_match_detail', pk=self.object.pk)

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['scheduled_match'] = self.object
        return context

    def get_success_url(self):
        if self.object.championship:
            return reverse_lazy('pingpong:championship_detail', kwargs={'pk': self.object.championship.pk})
        return reverse_lazy('pingpong:scheduled_match_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        messages.success(self.request, "Scheduled match updated successfully.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Live Scoreboard (KAN-4)
# ---------------------------------------------------------------------------



class _Refused(Exception):
    """A live request answered with a JSON error rather than a 403/404.

    Raised inside the op's transaction (so it rolls back) and turned into
    its response by ``_run_live``.
    """

    def __init__(self, response: JsonResponse):
        super().__init__(response.content)
        self.response = response


@dataclass
class _LiveCaller:
    """Who is driving a live request, and which op stream is theirs.

    ``scorer`` is the stream the caller's seqs are checked against: their own
    row, the sole active scorer's for staff on a solo match, or ``None`` for
    a row-less (legacy) match. ``own`` is the scorer only when it is the
    caller's own row -- presence and ``me`` are never about somebody else.
    """

    player: Player
    scorer: LiveScorer | None

    @property
    def own(self) -> LiveScorer | None:
        if self.scorer is not None and self.scorer.player_id == self.player.pk:
            return self.scorer
        return None


def _authorize_scorer(request, match: Match, player: Player, *, writing: bool) -> _LiveCaller:
    """KAN-149: a non-revoked ``LiveScorer`` row (or, on a row-less match,
    being its ``scorekeeper``), or staff.

    Staff keep the bypass for viewing, and may still drive a solo match as
    before (through its one stream), but a staff user without a row posting
    into a *shared* match is refused with 409 ``not a scorer``: an
    unattributed write into a two-writer match is undebuggable.
    """
    if not match.live_scorers.all():
        # Legacy shape: a match finished before KAN-149, or a hand-built one.
        if match.scorekeeper_id == player.pk or request.user.is_staff:
            return _LiveCaller(player, None)
        raise PermissionDenied("Only the match scorekeeper can use the scoreboard.")

    own = live_ops.scorer_for(match, player)
    if own is not None and own.revoked_at is None:
        return _LiveCaller(player, own)
    if request.user.is_staff:
        active = live_ops.active_scorers(match)
        if writing and len(active) > 1:
            raise _Refused(JsonResponse({"error": "not a scorer"}, status=409))
        return _LiveCaller(player, active[0] if len(active) == 1 else None)
    raise PermissionDenied("Only this match's scorers can use the scoreboard.")


def _get_live_match_for_scorer(request, pk, *, writing: bool = False):
    """Load a live match and authorize the caller as one of its scorers.

    Returns ``(match, _LiveCaller)``; raises Http404 (another club's match, or
    none) or PermissionDenied, or ``_Refused`` for a staff write into a
    shared match. The scorer rows come with the match, in one prefetch.
    """
    # all_objects so the default manager's is_live=False filter doesn't hide
    # the target -- but scoped, because bypassing the *user* filter for a
    # scorer is not a reason to expose another club's match.
    match = get_object_or_404(
        scope_to_active_club(
            Match.all_objects.prefetch_related(live_ops.scorers_prefetch())
        ),
        pk=pk,
    )
    try:
        user_player = request.user.player
    except (AttributeError, Player.DoesNotExist):
        raise PermissionDenied("You must have a player profile.")
    return match, _authorize_scorer(request, match, user_player, writing=writing)


def _lock_live_match(request, pk):
    """Authorize, then lock the match row, refresh, and re-read the scorer
    rows under the lock (KAN-77 order: lock -> refresh -> seq -> is_live).

    The authorization is re-run on the re-read rows, so a scorer revoked (or
    a guest who joined) while this request waited for the lock is judged on
    what is true now. Must run inside ``transaction.atomic()``.
    """
    match, caller = _get_live_match_for_scorer(request, pk, writing=True)
    Match.all_objects.select_for_update().get(pk=match.pk)
    match.refresh_from_db()
    live_ops.load_scorers(match)
    caller = _authorize_scorer(request, match, caller.player, writing=True)
    live_ops.touch(caller.own)
    return match, caller


def _run_live(request, pk, apply) -> JsonResponse:
    """The frame every live write runs in: one transaction, the lock, the
    caller's stream, and one write of that stream at the end.

    ``apply(match, caller)`` returns the response; the scorer row (consumed
    seq + due presence stamp) is saved before the transaction commits.
    """
    try:
        with transaction.atomic():
            match, caller = _lock_live_match(request, pk)
            response = apply(match, caller)
            live_ops.save_scorer(caller.scorer)
            return response
    except _Refused as refused:
        return refused.response


def _scoreboard_payload(
    match: Match,
    *,
    caller: _LiveCaller | None = None,
    redirect_url: str | None = None,
) -> dict:
    """JSON-friendly payload for the scoreboard client.

    ``applied_seq`` is the caller's own stream; ``rev`` is match-wide;
    ``scorers`` is presence for every phone; ``me`` is the caller's row.
    """
    state = match.live_state or {}
    scorer = caller.scorer if caller else None
    own = caller.own if caller else None
    return {
        "is_live": match.is_live,
        "is_match_complete": not match.is_live and match.live_state is None,
        "state": state,
        "current_server": live_scoring.current_server(state) if state.get("started") else None,
        "should_prompt_side_switch": (
            live_scoring.should_prompt_side_switch(state) if state.get("started") else False
        ),
        "redirect_url": redirect_url,
        "applied_seq": live_ops.applied_seq(match, scorer),
        "rev": match.live_rev,
        "scorers": live_ops.presence(match),
        "me": (
            {"side": own.side, "role": own.role, "can_score_both": own.can_score_both}
            if own is not None
            else None
        ),
    }


# --- Op sequence numbers (KAN-77, per scorer since KAN-149) ----------------
#
# Every live op may carry a client-assigned ``seq``: per scorer, starting at
# 1, gapless. The scorer row's ``applied_seq`` (``Match.live_applied_seq`` on
# a row-less legacy match) is the highest one applied. The check
# runs right after the row lock + refresh and BEFORE the ``is_live`` check, so
# a retried match point that arrives after the match completed is answered
# with the completion rather than "Match is not live".


def _parse_seq(payload) -> tuple[int | None, JsonResponse | None]:
    """``(seq, None)``, ``(None, None)`` for a legacy body, or a 400."""
    seq = payload.get("seq") if isinstance(payload, dict) else None
    if seq is None:
        return None, None
    # bool is an int subclass: ``true`` must not pass as seq 1.
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return None, JsonResponse({"error": "seq must be an integer >= 1"}, status=400)
    return seq, None


def _classify_seq(applied: int, seq: int) -> str:
    """``"apply"``, ``"duplicate"`` or ``"gap"`` for an op against ``applied``."""
    if seq <= applied:
        return "duplicate"
    if seq > applied + 1:
        return "gap"
    return "apply"


def _completion_redirect(match: Match) -> str | None:
    """Where a client learning that the match finished should go next."""
    if not match.is_live and match.winner_side is not None:
        return reverse("pingpong:match_confirm", args=[match.pk])
    return None


def _check_seq(
    match: Match, caller: _LiveCaller, seq: int | None, op_type: str
) -> JsonResponse | None:
    """Answer a duplicate or a gap; ``None`` means go ahead and apply.

    The match must already be locked and refreshed, and the seq is judged
    against the **caller's** stream.
    """
    if seq is None:
        if match.is_shared:
            # An unsequenced op cannot be deduped against two streams. Only
            # a page cached from before KAN-77 sends one; it must reload.
            return JsonResponse({"error": "reload"}, status=409)
        # Legacy client (a page cached from before KAN-77). Applied
        # unconditionally, exactly as before; remove once nothing sends it.
        logger.info(
            "Live %s without seq for match %s (legacy client)", op_type, match.pk
        )
        return None
    applied = live_ops.applied_seq(match, caller.scorer)
    verdict = _classify_seq(applied, seq)
    if verdict == "duplicate":
        return JsonResponse(
            _scoreboard_payload(
                match, caller=caller, redirect_url=_completion_redirect(match)
            )
        )
    if verdict == "gap":
        return JsonResponse({"error": "gap", "applied_seq": applied}, status=409)
    return None


def _apply_op(match: Match, caller: _LiveCaller, op: dict) -> JsonResponse:
    """Apply one deduped op through ``live_ops`` and shape the response.

    ``_record_point_event`` is looked up here, at call time, so a test can
    substitute a broken writer on this module.
    """
    result = live_ops.apply_op(
        match, op, scorer=caller.scorer, record_point=_record_point_event
    )
    if not result.applied:
        return _op_error(match, caller, op.get("seq"), result.error, result.status)
    return JsonResponse(
        _scoreboard_payload(match, caller=caller, redirect_url=result.redirect_url)
    )


def _op_error(
    match: Match, caller: _LiveCaller, seq: int | None, error: str, status: int = 409
) -> JsonResponse:
    """An op's error body; a sequenced op also learns its ``applied_seq``."""
    body: dict[str, Any] = {"error": error}
    if seq is not None:
        body["applied_seq"] = live_ops.applied_seq(match, caller.scorer)
    return JsonResponse(body, status=status)


def _run_op(request, pk, seq: int | None, op_type: str, apply) -> JsonResponse:
    """A single-op endpoint: dedupe against the caller's stream, then
    ``apply(match, caller)``."""
    return _run_live(
        request,
        pk,
        lambda match, caller: (
            _check_seq(match, caller, seq, op_type) or apply(match, caller)
        ),
    )


def _lenient_json(request) -> dict:
    """Body of an endpoint that historically ignored it (undo, side switch)."""
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# Kept under its historical name: the fidelity harness substitutes broken
# writers here to prove it notices them.
_record_point_event = live_ops.record_point_event


class LiveScoreboardView(LoginRequiredMixin, View):
    """Render the scoreboard page (KAN-27 fills in the template)."""

    template_name = "pingpong/scoreboard.html"

    def get(self, request, pk):
        match, caller = _get_live_match_for_scorer(request, pk)
        if not match.is_live:
            messages.info(request, "This match is no longer live.")
            return redirect("pingpong:match_detail", pk=pk)

        cache_side_players(match)

        # Games needed to take the match. The scoreboard shows them as that
        # many small bars rather than a "2/3" fraction, because the whole
        # screen is read from a metre away while holding a paddle -- bars are
        # countable at a glance and a fraction is not.
        games_to_win = match.best_of // 2 + 1

        team1_label = " & ".join(p.name for p in match.cached_team1_players)
        team2_label = " & ".join(p.name for p in match.cached_team2_players)
        team1_hue = (
            match.cached_team1_players[0].hue_class
            if match.cached_team1_players else "hue-1"
        )
        team2_hue = (
            match.cached_team2_players[0].hue_class
            if match.cached_team2_players else "hue-2"
        )
        context = {
            "match": match,
            # (live side, label, hue class): the two tap zones render from one
            # loop, so they cannot drift apart.
            "sides": [("team1", team1_label, team1_hue), ("team2", team2_label, team2_hue)],
            "team1_label": team1_label,
            "team2_label": team2_label,
            # Each half of the scoreboard wears its player's permanent hue, so
            # the two sides are the two people rather than "left" and "right".
            "team1_hue_class": team1_hue,
            "team2_hue_class": team2_hue,
            "games_to_win": games_to_win,
            "game_pips": range(games_to_win),
            "bootstrap": _scoreboard_payload(match, caller=caller),
            "point_url": reverse("pingpong:live_point", args=[pk]),
            "start_url": reverse("pingpong:live_start", args=[pk]),
            "state_url": reverse("pingpong:live_state", args=[pk]),
            "undo_url": reverse("pingpong:live_undo", args=[pk]),
            "side_switch_url": reverse("pingpong:live_side_switch", args=[pk]),
            "sync_url": reverse("pingpong:live_sync", args=[pk]),
            # For static/pingpong/js/scoreboard.js, via json_script. The user
            # id scopes the offline outbox: two accounts on one phone must
            # never share one (KAN-80).
            "scoreboard_config": {
                "match_pk": match.pk,
                "user_id": request.user.pk,
                "best_of": match.best_of,
                "max_batch": SYNC_MAX_OPS,
                "login_url": "{}?{}".format(
                    resolve_url(settings.LOGIN_URL),
                    urlencode({"next": request.get_full_path()}),
                ),
            },
        }
        response = render(request, self.template_name, context)
        # The service worker keeps this page for offline reopening, filed under
        # whose page it is (pingpong/offline.py). No header, no copy.
        response[offline.IDENTITY_HEADER] = offline.request_identity(request)
        return response


@method_decorator(require_POST, name="dispatch")
class LiveStartView(LoginRequiredMixin, View):
    """Initialize live_state with the picked initial server."""

    def post(self, request, pk):
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        server = payload.get("initial_server")
        if server not in ("team1", "team2"):
            return JsonResponse({"error": "initial_server must be 'team1' or 'team2'"}, status=400)
        seq, error = _parse_seq(payload)
        if error:
            return error

        def apply(match, caller):
            # Legacy clients only: idempotent if already started with the
            # same server. A sequenced re-send is a duplicate and was already
            # answered by _check_seq.
            if (
                seq is None
                and match.is_live
                and (match.live_state or {}).get("started")
                and match.live_state.get("initial_server") == server
            ):
                return JsonResponse(_scoreboard_payload(match, caller=caller))
            return _apply_op(
                match, caller, {"type": "start", "seq": seq, "initial_server": server}
            )

        # The lock (in _run_op) serializes concurrent requests.
        return _run_op(request, pk, seq, "start", apply)


@method_decorator(require_POST, name="dispatch")
class LivePointView(LoginRequiredMixin, View):
    """Add a point. Server validates rules and returns canonical state."""

    def post(self, request, pk):
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        side = payload.get("side")
        if side not in ("team1", "team2"):
            return JsonResponse({"error": "side must be 'team1' or 'team2'"}, status=400)
        seq, error = _parse_seq(payload)
        if error:
            return error

        op = {
            "type": "point",
            "seq": seq,
            "side": side,
            "client_ts": payload.get("client_ts"),
        }
        return _run_op(
            request, pk, seq, "point",
            lambda match, caller: _apply_op(match, caller, op),
        )


class LiveStateView(LoginRequiredMixin, View):
    """GET the canonical state, for the scoreboard to resync against.

    The page calls this when the tab becomes visible again: a phone that
    slept, or a match finished from another device, would otherwise leave
    the scoreboard showing a score the server no longer agrees with.
    Restricted to the match's scorers (and staff), like the write endpoints,
    and the caller's presence stamp is refreshed here (throttled).
    """

    def get(self, request, pk):
        # Scoped all_objects inside, so a completed match's redirect can be
        # returned too without reaching across clubs.
        match, caller = _get_live_match_for_scorer(request, pk)
        live_ops.touch(caller.own)
        live_ops.save_scorer(caller.own)

        redirect_url = None
        if not match.is_live:
            redirect_url = reverse("pingpong:match_detail", args=[match.pk])
        payload = _scoreboard_payload(match, caller=caller, redirect_url=redirect_url)
        # A scoreboard served from the service worker's cache may carry a
        # token from an older CSRF secret; this one matches the cookie the
        # browser holds now, so the client can retry a refused sync (KAN-81).
        payload["csrf_token"] = get_token(request)
        return JsonResponse(payload)


@method_decorator(require_POST, name="dispatch")
class LiveSideSwitchView(LoginRequiredMixin, View):
    """Mark the deciding-game side switch as confirmed (KAN-9)."""

    def post(self, request, pk):
        seq, error = _parse_seq(_lenient_json(request))
        if error:
            return error

        return _run_op(
            request, pk, seq, "side_switch",
            lambda match, caller: _apply_op(
                match, caller, {"type": "side_switch", "seq": seq}
            ),
        )


@method_decorator(require_POST, name="dispatch")
class LiveUndoView(LoginRequiredMixin, View):
    """Undo the most recent point (KAN-10); see ``live_ops._undo``."""

    def post(self, request, pk):
        seq, error = _parse_seq(_lenient_json(request))
        if error:
            return error

        return _run_op(
            request, pk, seq, "undo",
            lambda match, caller: _apply_op(match, caller, {"type": "undo", "seq": seq}),
        )


SYNC_MAX_OPS = 500


def _parse_sync_ops(request) -> tuple[list[dict] | None, JsonResponse | None]:
    """Validate a ``live_sync`` body; ``(ops sorted by seq, None)`` or an error.

    The whole batch is validated before anything is applied: a malformed op
    is a client bug, not a rule rejection, and must not half-apply a batch.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("ops"), list):
        return None, JsonResponse({"error": "ops must be a list"}, status=400)

    raw_ops = payload["ops"]
    if len(raw_ops) > SYNC_MAX_OPS:
        return None, JsonResponse(
            {"error": f"at most {SYNC_MAX_OPS} ops per batch"}, status=413
        )

    ops = []
    seen = set()
    for raw in raw_ops:
        if not isinstance(raw, dict):
            return None, JsonResponse({"error": "each op must be an object"}, status=400)
        seq, error = _parse_seq(raw)
        if error:
            return None, error
        if seq is None:
            return None, JsonResponse({"error": "every op needs a seq"}, status=400)
        if seq in seen:
            return None, JsonResponse({"error": f"seq {seq} appears twice"}, status=400)
        seen.add(seq)

        op_type = raw.get("type")
        if op_type not in live_ops.OP_TYPES:
            return None, JsonResponse(
                {"error": f"op {seq}: type must be one of {', '.join(live_ops.OP_TYPES)}"},
                status=400,
            )
        op = {"type": op_type, "seq": seq}
        if op_type == "point":
            if raw.get("side") not in ("team1", "team2"):
                return None, JsonResponse(
                    {"error": f"op {seq}: side must be 'team1' or 'team2'"}, status=400
                )
            op["side"] = raw["side"]
            # Never validated: a bad clock is stored as null, not refused.
            op["client_ts"] = raw.get("client_ts")
        elif op_type == "start":
            if raw.get("initial_server") not in ("team1", "team2"):
                return None, JsonResponse(
                    {"error": f"op {seq}: initial_server must be 'team1' or 'team2'"},
                    status=400,
                )
            op["initial_server"] = raw["initial_server"]
        ops.append(op)

    ops.sort(key=lambda op: op["seq"])
    return ops, None


@method_decorator(require_POST, name="dispatch")
class LiveSyncView(LoginRequiredMixin, View):
    """Apply an ordered batch of live ops in one request (KAN-88).

    After a dead spell the scoreboard may hold dozens of unsent ops; replaying
    them one round-trip at a time on slow 4G takes minutes. This applies them
    in one transaction under one row lock, in ``seq`` order, through the same
    ``live_ops.apply_op`` as the single-op endpoints:

    * ops at or below the applied seq are skipped (duplicate deliveries);
    * a gap stops processing -- 409 ``{"error": "gap", "applied_seq": N}``
      when nothing new was applied, else 200 with it named in ``rejected``;
    * the first rule rejection stops processing and is named in ``rejected``;
      the ops before it stay applied, and it consumes its seq;
    * once an op completes the match, the next one is rejected with
      ``"match complete"``, and ``redirect_url`` points at match_confirm.
    """

    def post(self, request, pk):
        ops, error = _parse_sync_ops(request)
        if error:
            return error

        return _run_live(request, pk, lambda match, caller: self._apply(match, caller, ops))

    @staticmethod
    def _apply(match, caller, ops) -> JsonResponse:
        rejected = None
        progressed = False
        for op in ops:
            applied = live_ops.applied_seq(match, caller.scorer)
            verdict = _classify_seq(applied, op["seq"])
            if verdict == "duplicate":
                continue
            if verdict == "gap":
                if not progressed:
                    return JsonResponse(
                        {"error": "gap", "applied_seq": applied}, status=409
                    )
                rejected = {"seq": op["seq"], "error": "gap"}
                break
            if not match.is_live and match.winner_side is not None:
                rejected = {"seq": op["seq"], "error": "match complete"}
                break

            result = live_ops.apply_op(
                match, op, scorer=caller.scorer, record_point=_record_point_event
            )
            progressed = True
            if not result.applied:
                rejected = {"seq": op["seq"], "error": result.error}
                break

        payload = _scoreboard_payload(
            match, caller=caller, redirect_url=_completion_redirect(match)
        )
        payload["rejected"] = rejected
        return JsonResponse(payload)


# ---------------------------------------------------------------------------
# PWA + Web Push
# ---------------------------------------------------------------------------

class ServiceWorkerView(TemplateView):
    """Serve the service worker from the site root.

    This is a Django view rather than a static file on purpose. A worker's
    default scope is the directory it was served from, so `/static/.../sw.js`
    could only ever control `/static/` -- useless, since the app lives under
    `/pingpong/`. Serving it from `/sw.js` gives it scope `/`, which covers
    everything including the `/accounts/` login pages.
    """

    template_name = "pingpong/sw.js"
    content_type = "application/javascript"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        urls = offline.precache_urls()
        # json.dumps, not a template loop: the list lands in a JS literal.
        ctx["sw_precache_json"] = json.dumps(urls)
        ctx["sw_build"] = offline.build_key(urls)
        return ctx


class ManifestView(TemplateView):
    """The web app manifest, rendered so icon URLs go through {% static %}.

    Hardcoding `/static/...` here would break under prod's hashing manifest
    storage, where every filename carries a content hash.
    """

    template_name = "pingpong/manifest.webmanifest"
    content_type = "application/manifest+json"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update({
            "app_name": settings.PWA_APP_NAME,
            "app_short_name": settings.PWA_APP_SHORT_NAME,
            "app_description": settings.PWA_APP_DESCRIPTION,
            "theme_color": settings.PWA_THEME_COLOR,
            "background_color": settings.PWA_BACKGROUND_COLOR,
            "start_url": reverse("pingpong:dashboard"),
        })
        return ctx


def _subscription_from_request(request):
    """Pull the browser's PushSubscription JSON out of the request body.

    Returns (endpoint, p256dh, auth) or raises ValueError with a message safe
    to hand back to the client.
    """
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raise ValueError("Malformed JSON body")

    endpoint = data.get("endpoint")
    keys = data.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")

    if not endpoint or not p256dh or not auth:
        raise ValueError("Subscription must have an endpoint and both keys")
    return endpoint, p256dh, auth


@method_decorator(login_required, name="dispatch")
class PushSubscribeView(View):
    """Register (or re-register) this browser for push.

    Upserts on `endpoint`: browsers hand back the same endpoint for the same
    device, and re-subscribing is routine -- it happens on every page load in
    push.js so a subscription the browser silently rotated gets repaired.
    Creating a new row each time would leave a user with dozens of them and
    make every notification arrive N times.
    """

    def post(self, request):
        try:
            endpoint, p256dh, auth = _subscription_from_request(request)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        subscription, created = PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                "user": request.user,
                "p256dh": p256dh,
                "auth": auth,
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
                # A re-subscribe is a fresh start: whatever made this endpoint
                # fail before is no longer interesting.
                "failure_count": 0,
            },
        )
        return JsonResponse({"ok": True, "created": created, "id": subscription.pk})


@method_decorator(login_required, name="dispatch")
class PushUnsubscribeView(View):
    """Drop this browser's subscription. Idempotent by design.

    Unsubscribing something already gone is a success, not a 404 -- the client
    calls this after the browser has already discarded its own subscription,
    so the two can legitimately disagree about whether the row still exists.
    """

    def post(self, request):
        try:
            data = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Malformed JSON body"}, status=400)

        endpoint = data.get("endpoint")
        qs = PushSubscription.objects.filter(user=request.user)
        if endpoint:
            qs = qs.filter(endpoint=endpoint)
        deleted, _ = qs.delete()
        return JsonResponse({"ok": True, "deleted": deleted})


@method_decorator(login_required, name="dispatch")
@method_decorator(
    ratelimit(key="user", rate="10/h", method="POST", block=True), name="dispatch"
)
class PushTestView(View):
    """Send the current user a test notification.

    Worth its own endpoint: "did this actually work?" is otherwise
    unanswerable without playing a match, and permission prompts fail in
    enough browser-specific ways that a one-tap check saves real debugging.

    Ignores notification preferences -- the user pressed the button, so they
    want this one regardless of what they have muted.
    """

    def post(self, request):
        delivered = push.send_to_user(
            request.user,
            kind=NotificationKind.MATCH_RESULT,
            title="TTStats",
            body="Push notifications are working. \U0001F3D3",
            url=reverse("pingpong:notification_settings"),
            tag="test",
            respect_preferences=False,
        )
        return JsonResponse({"ok": bool(delivered), "delivered": delivered})


class NotificationSettingsView(LoginRequiredMixin, View):
    """Enable/disable push on this device, and mute individual notifications.

    The per-kind toggles are server state; whether *this browser* is
    subscribed is browser state that only JavaScript can see. The template
    renders both, and push.js fills in the device half on load.
    """

    template_name = "pingpong/notification_settings.html"

    def _context(self, request):
        prefs = NotificationPreference.for_user(request.user)
        return {
            "prefs": prefs,
            "kinds": [
                (kind.value, kind.label, prefs.wants(kind.value))
                for kind in NotificationKind
            ],
            "subscriptions": request.user.push_subscriptions.all(),
            "vapid_public_key": settings.VAPID_PUBLIC_KEY,
            "webpush_enabled": push.webpush_enabled(),
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        prefs = NotificationPreference.for_user(request.user)
        for kind in NotificationKind:
            # An unchecked checkbox sends nothing, so absence means off. This
            # only works because the form posts every toggle every time.
            setattr(prefs, f"push_{kind.value}", kind.value in request.POST)
        prefs.save()
        messages.success(request, "Notification preferences saved")
        return redirect("pingpong:notification_settings")


# ---------------------------------------------------------------------------
# Club administration (KAN-17 / S1.7)
# ---------------------------------------------------------------------------


class ClubAdminRequiredMixin(LoginRequiredMixin):
    """Gate a view on the viewer being an admin of their active club.

    One helper, used by every club-admin view. The ticket's requirement is
    precise about this: a role check must not be an inline comparison copied
    per view, because the copy that gets missed is the one that matters.

    Raises `PermissionDenied` (403) rather than redirecting, so a forged POST
    is refused rather than quietly bounced to a page that looks like success.
    Templates are still expected not to render controls the viewer cannot use
    -- the 403 is a correctness backstop, not the UX.
    """

    def get_active_club(self):
        from ttstats.middleware import get_current_club

        club = get_current_club()
        if club is None:
            raise Http404("No active club.")
        return club

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)

        self.club = self.get_active_club()
        if not self.club.user_can_administer(request.user):
            raise PermissionDenied(
                "Only a club admin or owner can do that."
            )
        return super().dispatch(request, *args, **kwargs)


class ClubSettingsView(ClubAdminRequiredMixin, View):
    """Edit the active club, and manage its members' roles."""

    template_name = "pingpong/club_settings.html"

    def get(self, request):
        return render(request, self.template_name, self._context())

    def _context(self, form=None):
        from .forms import ClubSettingsForm

        memberships = list(self.club.memberships.select_related("user").all())
        viewer = self.club.membership_for(self.request.user)

        # Which rows the viewer may actually act on, decided HERE rather than
        # in template logic. The template must not render a control the viewer
        # cannot use -- a 403 on submit is a correctness backstop, not the UX --
        # and the rules (outranking, last-owner) are the same ones the POST
        # handlers enforce, so they are computed once and read twice.
        owners = sum(1 for m in memberships if m.is_owner)
        actionable_ids = {
            m.pk
            for m in memberships
            if viewer.outranks(m) and not (m.is_owner and owners <= 1)
        }

        from .forms import ClubInviteForm

        return {
            "club": self.club,
            "form": form or ClubSettingsForm(instance=self.club),
            "invite_form": ClubInviteForm(),
            "invites": self.club.invites.all()[:10],
            # Popped, not read: an invite link is a bearer credential, so it is
            # shown once on the redirect after creation rather than living on
            # the settings page forever.
            "last_invite_url": self.request.session.pop("last_invite_url", None),
            "memberships": memberships,
            "viewer_membership": viewer,
            "actionable_ids": actionable_ids,
            "role_choices": ClubRole.choices,
        }

    def post(self, request):
        from .forms import ClubSettingsForm

        form = ClubSettingsForm(request.POST, instance=self.club)
        if form.is_valid():
            form.save()
            messages.success(request, "Club settings saved.")
            return redirect("pingpong:club_settings")
        return render(request, self.template_name, self._context(form))


class ClubMemberRoleView(ClubAdminRequiredMixin, View):
    """Change one member's role.

    Two rules, both enforced here rather than in the template:

    * You cannot act on someone who outranks you or is your equal. An admin
      promoting themselves past the owner would make the owner role meaningless.
    * The last owner cannot be demoted. A club with no owner has no one who can
      appoint one, which is unrecoverable without a shell.
    """

    def post(self, request, membership_id):
        membership = get_object_or_404(
            self.club.memberships.select_related("user"), pk=membership_id
        )
        viewer = self.club.membership_for(request.user)
        new_role = request.POST.get("role")

        if new_role not in dict(ClubRole.choices):
            messages.error(request, "Unknown role.")
            return redirect("pingpong:club_settings")

        if membership.pk != viewer.pk and not viewer.outranks(membership):
            messages.error(
                request,
                f"You cannot change the role of a club {membership.get_role_display().lower()}.",
            )
            return redirect("pingpong:club_settings")

        if (
            membership.is_owner
            and new_role != ClubRole.OWNER
            and self.club.owner_count <= 1
        ):
            messages.error(
                request,
                "This is the club's only owner. Promote someone else to owner first.",
            )
            return redirect("pingpong:club_settings")

        if new_role == ClubRole.OWNER and not viewer.is_owner:
            messages.error(request, "Only an owner can appoint another owner.")
            return redirect("pingpong:club_settings")

        membership.role = new_role
        membership.save(update_fields=["role"])
        messages.success(
            request,
            f"{membership.user.username} is now a club {membership.get_role_display().lower()}.",
        )
        return redirect("pingpong:club_settings")


class ClubMemberRemoveView(ClubAdminRequiredMixin, View):
    """Remove a member from the club.

    The last owner cannot be removed -- including by themselves. "I am the only
    owner and I left" is the one mistake with no in-app recovery.
    """

    def post(self, request, membership_id):
        membership = get_object_or_404(
            self.club.memberships.select_related("user"), pk=membership_id
        )
        viewer = self.club.membership_for(request.user)

        if membership.pk != viewer.pk and not viewer.outranks(membership):
            messages.error(
                request,
                "You cannot remove someone with an equal or higher role.",
            )
            return redirect("pingpong:club_settings")

        if membership.is_owner and self.club.owner_count <= 1:
            messages.error(
                request,
                "A club must always have an owner. Promote someone else first.",
            )
            return redirect("pingpong:club_settings")

        username = membership.user.username
        membership.delete()
        messages.success(request, f"{username} was removed from the club.")
        return redirect("pingpong:club_settings")


# ---------------------------------------------------------------------------
# Club invites (KAN-18 / S1.6)
# ---------------------------------------------------------------------------

# Where an invite token waits while an anonymous visitor goes through signup.
# Session rather than a query parameter on the signup URL: the registration
# form posts to itself, so a query parameter would be lost on the first
# validation error and the invite would silently evaporate.
PENDING_INVITE_SESSION_KEY = "pending_club_invite"


def _find_invite(token):
    """Look an invite up by token. Returns None rather than raising.

    Compared in constant time. A plain `filter(token=token)` on an indexed
    unique column is already not a timing oracle in any practical sense, but
    the lookup result is then re-verified with `secrets.compare_digest` so the
    property does not depend on how the database happens to index the column.

    The token is never logged, here or anywhere -- it is a bearer credential.
    """
    import secrets

    if not token or len(token) > 64:
        return None
    invite = ClubInvite.objects.filter(token=token).select_related("club").first()
    if invite is None:
        return None
    if not secrets.compare_digest(str(invite.token), str(token)):
        return None
    return invite


def accept_club_invite(request, invite, user=None):
    """Put `user` (default: the request's user) in the invite's club.

    One place, called from both the accept view and the post-signup hook, so
    the two paths cannot drift about what "joined" means.

    The explicit `user` argument is not optional decoration: at registration
    time the new account exists but is **not logged in yet**, so `request.user`
    is still anonymous and the invite would silently do nothing.
    """
    user = user or request.user
    if not user or not user.is_authenticated:
        return "invalid"

    if invite.club.user_is_member(user):
        return "already_member"

    if not invite.claim():
        return "invalid"

    ClubMembership.objects.get_or_create(
        club=invite.club,
        user=user,
        defaults={"role": ClubRole.MEMBER},
    )

    # base.html renders {% url 'pingpong:player_detail' user.player.pk %}
    # unconditionally, so a member without a Player 500s every page in the app.
    # An invite must never leave someone in that state.
    player = getattr(user, "player", None)
    if player is None:
        Player.objects.create(user=user, name=user.username, club=invite.club)
    elif player.club_id is None:
        Player.objects.filter(pk=player.pk).update(club=invite.club)

    from ttstats.middleware import set_active_club

    set_active_club(request, invite.club)
    return "joined"


class ClubInviteCreateView(ClubAdminRequiredMixin, View):
    """Generate an invite link for the active club. Admins only."""

    def post(self, request):
        from .forms import ClubInviteForm

        form = ClubInviteForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Could not create that invite.")
            return redirect("pingpong:club_settings")

        invite = ClubInvite.issue(
            club=self.club,
            created_by=request.user,
            validity=timedelta(days=form.cleaned_data["valid_for_days"]),
            max_uses=form.cleaned_data.get("max_uses") or None,
            note=form.cleaned_data.get("note", ""),
        )
        messages.success(
            request,
            "Invite link created. Share it with the person you want to add.",
        )
        request.session["last_invite_url"] = request.build_absolute_uri(
            reverse("pingpong:club_invite_accept", args=[invite.token])
        )
        return redirect("pingpong:club_settings")


class ClubInviteRevokeView(ClubAdminRequiredMixin, View):
    """Revoke an invite. Admins only, and only for their own club."""

    def post(self, request, invite_id):
        invite = get_object_or_404(self.club.invites, pk=invite_id)
        invite.revoke()
        messages.success(request, "That invite link no longer works.")
        return redirect("pingpong:club_settings")


@method_decorator(
    # A token is guessable only by brute force, so the accept endpoint is the
    # one place worth rate limiting: it is the only oracle for "is this token
    # real". Keyed on IP because the attacker is by definition not logged in.
    ratelimit(key="ip", rate="20/h", method="GET", block=True),
    name="dispatch",
)
class ClubInviteAcceptView(View):
    """Open an invite link.

    Handles both visitors: an authenticated user joins immediately, an
    anonymous one is sent through the *existing* signup flow with the token
    parked in their session. Registration is not forked -- a second signup path
    would drift from the first, and this one already creates the Player,
    sends verification and logs the user in.
    """

    template_name = "pingpong/club_invite_invalid.html"

    def get(self, request, token):
        invite = _find_invite(token)

        if invite is None or not invite.is_valid:
            return self._invalid(request, invite)

        if not request.user.is_authenticated:
            # Park it and send them to signup. Survives the round trip,
            # including a failed validation pass on the form.
            request.session[PENDING_INVITE_SESSION_KEY] = invite.token
            messages.info(
                request,
                f"Create an account to join {invite.club.name}.",
            )
            return redirect("pingpong:signup")

        outcome = accept_club_invite(request, invite)

        if outcome == "already_member":
            messages.info(
                request, f"You are already a member of {invite.club.name}."
            )
        elif outcome == "invalid":
            # Lost the race for the last use of a single-use link.
            invite.refresh_from_db()
            return self._invalid(request, invite)
        else:
            messages.success(request, f"Welcome to {invite.club.name}!")

        return redirect("pingpong:dashboard")

    def _invalid(self, request, invite):
        """Never a 500, and never silent membership."""
        return render(
            request,
            self.template_name,
            {
                "reason": (
                    invite.invalid_reason
                    if invite is not None
                    else "This invite link is not valid."
                ),
                "club": invite.club if invite is not None else None,
            },
            status=404 if invite is None else 410,
        )


def consume_pending_invite(request, user=None):
    """Apply a parked invite after signup. Safe to call unconditionally.

    Called from `PlayerRegistrationView.form_valid`, which is why the invite
    flow does not need a registration path of its own. `user` is passed
    explicitly there because the new account is not logged in yet.
    """
    token = request.session.pop(PENDING_INVITE_SESSION_KEY, None)
    user = user or getattr(request, "user", None)
    if not token or not user or not user.is_authenticated:
        return None

    invite = _find_invite(token)
    if invite is None or not invite.is_valid:
        return None

    if accept_club_invite(request, invite, user=user) != "joined":
        return None
    return invite.club


class ClubSwitchView(LoginRequiredMixin, View):
    """Switch the viewer's active club.

    POST only. Switching changes what every subsequent page shows, which makes
    it a state change, not a navigation -- a GET link would let any page on the
    internet move someone's club with an <img> tag.

    Membership is checked here *and* re-checked by `_resolve_active_club` on
    every later request. This one is the courtesy error message; that one is
    the security boundary.
    """

    def post(self, request):
        from ttstats.middleware import set_active_club

        # Coerced before it reaches the ORM: a non-numeric pk raises ValueError
        # out of the field's get_prep_value, which would turn a malformed POST
        # into a 500 rather than a polite refusal.
        try:
            club_id = int(request.POST.get("club_id", ""))
        except (TypeError, ValueError):
            club_id = None

        club = (
            Club.objects.for_user(request.user).filter(pk=club_id).first()
            if club_id is not None
            else None
        )

        if club is None:
            messages.error(request, "You are not a member of that club.")
            return redirect("pingpong:dashboard")

        set_active_club(request, club)
        messages.success(request, f"Now viewing {club.name}.")
        # Today, deliberately. Staying put would leave the viewer on a detail
        # page belonging to the club they just left, which then 404s.
        return redirect("pingpong:dashboard")

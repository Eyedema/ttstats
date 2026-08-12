import json
import logging
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
from django.db.models import Count, F, Prefetch, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
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

from .forms import GameForm, MatchEditForm, MatchForm, PlayerRegistrationForm, ScheduledMatchForm, MatchConvertForm, \
    ChampionshipCreateForm, ChampionshipEditForm, ScheduledMatchEditForm
from .achievements import get_achievement_progress
from .elo import calculate_expected_score, get_win_probability
from .models import Game, Location, Match, MatchParticipant, Player, UserProfile, MatchConfirmation, ScheduledMatch, ScheduledMatchParticipant, Side, Team, Championship, EloHistory, format_side_label
from .emails import send_scheduled_match_email, send_passkey_deleted_email
from .services import resolve_sides

try:
    from django_otp_webauthn.models import WebAuthnCredential
except ImportError:
    WebAuthnCredential = None


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
            .select_related("team1", "team2", "location", "winner", "championship")
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

        # Get Elo changes for this match
        elo_changes = match.elo_history.select_related('player').all()

        # Pass separate elo changes for easier template access
        side_by_player = {
            p.player_id: p.side for p in match.participants.all()
        }
        for change in elo_changes:
            side = side_by_player.get(change.player_id)
            if side == Side.ONE:
                context['player1_elo_change'] = change
            elif side == Side.TWO:
                context['player2_elo_change'] = change

        # Win probability (uses pre-match Elo if EloHistory exists)
        t1_pct, t2_pct = get_win_probability(
            match.side1_players, match.side2_players, match=match
        )
        context['team1_win_pct'] = t1_pct
        context['team2_win_pct'] = t2_pct

        return context


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
        stats_cache_key = f'player_stats_{player.pk}'
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
            if _player_side(player, match) == Side.ONE:
                match.p1_score = match.team1_score
                match.p2_score = match.team2_score
            else:
                match.p1_score = match.team2_score
                match.p2_score = match.team1_score

            # Check if player won (works for both 1v1 and 2v2)
            match.player_won = _player_won(player, match)

        # Elo chart data (included in player_stats cache)
        if 'elo_chart_labels' in cached_stats:
            elo_labels = cached_stats['elo_chart_labels']
            elo_data = cached_stats['elo_chart_data']
        else:
            elo_history = list(
                EloHistory.objects.filter(player=player)
                .order_by('created_at')
                .values_list('created_at', 'new_rating')
            )
            if elo_history:
                elo_labels = ['Start'] + [entry[0].strftime('%Y-%m-%d') for entry in elo_history]
                elo_data = [1500] + [entry[1] for entry in elo_history]
            else:
                elo_labels = []
                elo_data = []
            # Update cache with elo chart data
            cached_stats['elo_chart_labels'] = elo_labels
            cached_stats['elo_chart_data'] = elo_data
            cache.set(stats_cache_key, cached_stats, 600)

        # Achievement data
        achievement_progress = get_achievement_progress(player)

        context.update({
            'matches': confirmed_matches_page.object_list,
            'page_obj': confirmed_matches_page,
            'is_paginated': paginator.num_pages > 1,
            'elo_chart_labels': elo_labels,
            'elo_chart_data': elo_data,
            'achievement_progress': achievement_progress,
            **cached_stats,
        })

        return context

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
            elif match.winner:  # Loss
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
    """Main dashboard view with statistics"""

    template_name = "pingpong/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Cache total players (15 min TTL)
        total_players = cache.get('dashboard_total_players')
        if total_players is None:
            total_players = Player.objects.count()
            cache.set('dashboard_total_players', total_players, 900)

        # Cache total confirmed matches using denormalized field (10 min TTL)
        total_matches = cache.get('dashboard_total_matches')
        if total_matches is None:
            total_matches = Match.objects.filter(is_confirmed=True).count()
            cache.set('dashboard_total_matches', total_matches, 600)

        # Recent matches (5 min TTL)
        recent_matches = cache.get('dashboard_recent_matches')
        if recent_matches is None:
            recent_matches = list(Match.objects.all().order_by("-date_played")[:5])
            cache.set('dashboard_recent_matches', recent_matches, 300)

        # Live matches I'm scorekeeping right now (KAN-11). Not cached —
        # this needs to stay fresh as the user scores points.
        live_matches = []
        try:
            user_player = self.request.user.player
            live_qs = Match.live_objects.filter(
                scorekeeper=user_player, is_live=True
            ).prefetch_related(participants_prefetch())
            for m in live_qs:
                state = m.live_state or {}
                cache_side_players(m)
                live_matches.append({
                    "pk": m.pk,
                    "opponent": " & ".join(
                        p.name for p in m.cached_team2_players
                    ),
                    "team1_label": " & ".join(
                        p.name for p in m.cached_team1_players
                    ),
                    "team1_games": state.get("team1_games", 0),
                    "team2_games": state.get("team2_games", 0),
                    "team1_points": state.get("team1_points", 0),
                    "team2_points": state.get("team2_points", 0),
                    "last_point_at": state.get("last_point_at"),
                    "best_of": m.best_of,
                    "resume_url": reverse(
                        "pingpong:live_scoreboard", args=[m.pk]
                    ),
                })
        except (AttributeError, Player.DoesNotExist):
            pass

        context.update(
            {
                "total_players": total_players,
                "total_matches": total_matches,
                "recent_matches": recent_matches,
                "live_matches": live_matches,
            }
        )

        return context


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
        if self.match.winner:
            messages.warning(
                request, f"This match is already complete. {self.match.winner} won!"
            )
            return redirect("pingpong:match_detail", pk=self.match.pk)

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["match"] = self.match
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
        if self.match.winner:
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
                f"🎉 Match Complete! {self.match.winner} wins {self.match.team1_score}-{self.match.team2_score}!", #TODO: "wins", but what if it is a team with 2 names?
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
                form.fields["player1"].widget.attrs.update(
                    {"class": "bg-muted cursor-not-allowed"}
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

        # Resolve the two sides to Team objects
        if is_double:
            team1, team2 = resolve_sides([player1, player2], [player3, player4])
        else:
            team1, team2 = resolve_sides([player1], [player2])

        # Assign teams to match instance (don't save yet)
        form.instance.team1 = team1
        form.instance.team2 = team2
        form.instance.is_double = is_double

        # "Score live" branch — KAN-7. Doubles deferred (KAN-13).
        self._start_live = bool(self.request.POST.get("start_live")) and not is_double
        if self._start_live:
            try:
                user_player = user.player
            except (AttributeError, Player.DoesNotExist):
                messages.error(self.request, "You need a player profile to score live.")
                return self.form_invalid(form)
            form.instance.is_live = True
            form.instance.scorekeeper = user_player
            form.instance.live_state = live_scoring.initial_state(form.instance.best_of)
            messages.success(self.request, "Live match started — tap a side to score.")
        else:
            messages.success(self.request, "Match created successfully!")

        return super().form_valid(form)


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
        if self.object.winner:
            return MatchEditForm
        return MatchForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["locations"] = Location.objects.all()
        context["is_complete"] = bool(self.object.winner)
        return context

    def get_success_url(self):
        return reverse_lazy("pingpong:match_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        if self.object.winner:
            messages.success(self.request, "Match details updated successfully!")
        else:
            messages.success(self.request, "Match updated successfully!")
        return super().form_valid(form)


class LeaderboardView(LoginRequiredMixin, TemplateView):
    """Display player rankings and statistics"""

    template_name = "pingpong/leaderboard.html"

    def _leaderboard_cache_key(self, match_type, date_filter, start_date, end_date, top_x):
        """Build a cache key that includes filter params and a generation counter."""
        generation = cache.get('leaderboard_generation', 0)
        return f'leaderboard_{generation}_{match_type}_{date_filter}_{start_date}_{end_date}_{top_x}'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get filter parameters
        match_type = self.request.GET.get('match_type', 'all')  # 'all', 'singles', 'doubles'
        date_filter = self.request.GET.get('date_filter', 'all')  # 'all', 'month', '3months', '6months', 'year', 'custom'
        start_date = self.request.GET.get('start_date', '')
        end_date = self.request.GET.get('end_date', '')
        top_x = self.request.GET.get('top_x', '10')

        # Try cache first (10 minute TTL)
        cache_key = self._leaderboard_cache_key(match_type, date_filter, start_date, end_date, top_x)
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            context["player_stats"] = cached_data
            context["top_x"] = top_x
            context["match_type"] = match_type
            context["date_filter"] = date_filter
            context["start_date"] = start_date
            context["end_date"] = end_date
            return context

        # Calculate date range
        from datetime import datetime, timedelta
        from django.utils import timezone

        filter_start_date = None
        filter_end_date = timezone.now()

        if date_filter == 'month':
            filter_start_date = filter_end_date - timedelta(days=30)
        elif date_filter == '3months':
            filter_start_date = filter_end_date - timedelta(days=90)
        elif date_filter == '6months':
            filter_start_date = filter_end_date - timedelta(days=180)
        elif date_filter == 'year':
            filter_start_date = filter_end_date - timedelta(days=365)
        elif date_filter == 'custom' and start_date and end_date:
            try:
                filter_start_date = timezone.make_aware(datetime.strptime(start_date, '%Y-%m-%d'))
                filter_end_date = timezone.make_aware(
                    datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59))
            except ValueError:
                # Invalid date format, default to all
                filter_start_date = None

        # Build base match query with filters applied at DB level
        # Use is_confirmed=True to filter at DB level instead of Python
        matches_query = Match._base_manager.filter(is_confirmed=True)

        # Apply match type filter at DB level
        if match_type == 'singles':
            matches_query = matches_query.filter(is_double=False)
        elif match_type == 'doubles':
            matches_query = matches_query.filter(is_double=True)

        # Apply date filter at DB level
        if filter_start_date:
            matches_query = matches_query.filter(
                date_played__gte=filter_start_date,
                date_played__lte=filter_end_date
            )

        # Prefetch only what we need for stats
        matches_query = matches_query.prefetch_related(
            participants_prefetch(), 'games'
        )

        # Load all confirmed matches once into memory
        confirmed_matches = list(matches_query)

        # Build a lookup dictionary: player_id -> list of their matches
        # This avoids N+1 queries
        player_matches = {}
        player_won_match = set()  # (player_id, match_id) pairs
        for match in confirmed_matches:
            for participant in match.participants.all():
                player_matches.setdefault(participant.player_id, []).append(match)
                if match.winner_side == participant.side:
                    player_won_match.add((participant.player_id, match.id))

        # Pre-cache game counts for all matches to avoid repeated queries
        match_game_counts = {}
        for match in confirmed_matches:
            # Games are already prefetched, so this uses cached data
            match_game_counts[match.id] = len(match.games.all())

        # Get only players that have matches (optimization)
        player_ids_with_matches = set(player_matches.keys())
        player_stats_qs = Player.objects.filter(
            id__in=player_ids_with_matches
        ).select_related('user')

        # Calculate stats in Python using pre-loaded data
        player_stats = []
        for player in player_stats_qs:
            # Get matches for this player from our lookup dict
            player_match_list = player_matches.get(player.id, [])

            if not player_match_list:
                continue

            total_matches = len(player_match_list)

            # Calculate wins using cached data
            wins = sum(
                1
                for match in player_match_list
                if (player.id, match.id) in player_won_match
            )

            losses = total_matches - wins
            win_rate = (wins / total_matches * 100) if total_matches > 0 else 0

            # Use pre-cached game counts
            total_games = sum(match_game_counts.get(m.id, 0) for m in player_match_list)

            player_stats.append({
                "player": player,
                "total_matches": total_matches,
                "total_games": total_games,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "elo_rating": player.elo_rating,
                "elo_peak": player.elo_peak,
            })

        # Sort by Elo rating (desc), then by total wins (desc), then by win rate (desc)
        player_stats.sort(
            key=lambda x: (x["elo_rating"], x["wins"], x["win_rate"]), reverse=True
        )

        # Apply top X filter
        try:
            top_x_int = int(top_x)
            if top_x_int > 0:
                player_stats = player_stats[:top_x_int]
        except (ValueError, TypeError):
            player_stats = player_stats[:10]

        # Cache for 10 minutes
        cache.set(cache_key, player_stats, 600)

        context["player_stats"] = player_stats
        context["top_x"] = top_x
        context["match_type"] = match_type
        context["date_filter"] = date_filter
        context["start_date"] = start_date
        context["end_date"] = end_date

        return context


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
            h2h_cache_key = f'h2h_{min(player1.pk, player2.pk)}_{max(player1.pk, player2.pk)}'
            cached_h2h = cache.get(h2h_cache_key)

            if cached_h2h is not None:
                context.update(cached_h2h)
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

                # Win probability based on current Elo
                p1_pct = round(calculate_expected_score(player1.elo_rating, player2.elo_rating) * 100)
                h2h_data["player1_win_pct"] = p1_pct
                h2h_data["player2_win_pct"] = 100 - p1_pct

                # Cache for 30 minutes
                cache.set(h2h_cache_key, h2h_data, 1800)
                context.update(h2h_data)
            else:
                p1_pct = round(calculate_expected_score(player1.elo_rating, player2.elo_rating) * 100)
                h2h_data = {
                    "player1": player1,
                    "player2": player2,
                    "has_data": False,
                    "has_2v2_matches": has_2v2_matches,
                    "player1_win_pct": p1_pct,
                    "player2_win_pct": 100 - p1_pct,
                }
                cache.set(h2h_cache_key, h2h_data, 1800)
                context.update(h2h_data)

        return context


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
                form.fields["player1"].widget.attrs.update(
                    {"class": "bg-muted cursor-not-allowed"}
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

        # Scheduled matches are singles-only for now
        team1, team2 = resolve_sides([player1], [player2])

        # Assign teams to scheduled match
        form.instance.team1 = team1
        form.instance.team2 = team2

        # Save the scheduled match
        self.object = form.save()
        scheduled_match = self.object

        # Send notification emails to both players
        send_scheduled_match_email(scheduled_match, player1)
        send_scheduled_match_email(scheduled_match, player2)

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
        ).select_related('match', 'match__team1', 'match__team2').prefetch_related(
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
            t1_pct, t2_pct = get_win_probability(
                sm.cached_team1_players, sm.cached_team2_players
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
        t1_pct, t2_pct = get_win_probability(
            scheduled_match.side1_players, scheduled_match.side2_players
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
        # Use _base_manager to bypass custom manager filtering
        try:
            self.scheduled_match = ScheduledMatch._base_manager.get(
                pk=self.kwargs['scheduled_match_pk']
            )
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

        # Resolve the two sides to Team objects. Note the pairing differs from
        # MatchCreateView: this form lays out side 1 as player1+player3.
        if is_double:
            team1, team2 = resolve_sides([player1, player3], [player2, player4])
        else:
            team1, team2 = resolve_sides([player1], [player2])

        # Assign teams to match
        form.instance.team1 = team1
        form.instance.team2 = team2
        form.instance.is_double = is_double

        # Save the match
        response = super().form_valid(form)

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
        return reverse_lazy("pingpong:dashboard")

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

class TeamsListView(LoginRequiredMixin, ListView):
    """View to list all players"""

    template_name = "pingpong/team_list.html"
    context_object_name = "teams"
    model = Team
    paginate_by = 10

    def get_queryset(self):
        # Annota il conteggio dei giocatori e filtra solo team con 2 giocatori
        return Team.objects.annotate(
            player_count=Count('players')
        ).filter(
            player_count=2
        ).prefetch_related('players').order_by('name')


class TeamUpdateView(LoginRequiredMixin, UpdateView):
    """View to update an existing team"""

    model = Team
    template_name = "pingpong/team_form.html"
    fields = ["name"]

    def get_success_url(self):
        return reverse_lazy("pingpong:team_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        messages.success(
            self.request, f"Team '{form.instance.name}' updated successfully!"
        )
        return super().form_valid(form)

class TeamDetailView(LoginRequiredMixin, DetailView):
    """View to show details of a single team"""

    template_name = "pingpong/team_detail.html"
    context_object_name = "team"
    model = Team

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        team = self.get_object()

        # Fetch confirmed matches for team using DB-level filtering
        confirmed_matches = list(Match.objects.filter(
            Q(team1=team) | Q(team2=team),
            is_confirmed=True,
        ).select_related('team1', 'team2', 'winner').prefetch_related(
            'team1__players',
            'team2__players',
            'games'
        ).order_by('-date_played').distinct())

        # Add custom attributes to each match from team's perspective
        for match in confirmed_matches:
            # Determine if team is team1 or team2
            is_team1 = match.team1 == team

            # Set opponent team
            match.opponent_team = match.team2 if is_team1 else match.team1

            # Set scores from team's perspective
            match.team_score = match.team1_score if is_team1 else match.team2_score
            match.opponent_score = match.team2_score if is_team1 else match.team1_score

            # Check if team won
            match.team_won = match.winner == team

        total_matches = len(confirmed_matches)

        # Won matches and losses
        wins = len([m for m in confirmed_matches if m.winner == team])
        losses = total_matches - wins

        # Calculate streaks
        stats = self._calculate_streaks(confirmed_matches, team)

        # Calculate performance statistics
        performance = self._calculate_performance_stats(confirmed_matches)

        context.update({
            'matches': confirmed_matches,
            'total_matches': total_matches,
            'wins': wins,
            'losses': losses,
            'win_rate': round((wins / total_matches * 100), 1) if total_matches > 0 else 0,
            'loss_rate': round((losses / total_matches * 100), 1) if total_matches > 0 else 0,
            'current_streak': stats['current_streak'],
            'longest_win_streak': stats['longest_win_streak'],
            'longest_loss_streak': stats['longest_loss_streak'],
            'avg_score': performance['avg_score'],
            'avg_opponent_score': performance['avg_opponent_score'],
            'best_win': performance['best_win'],
            'worst_loss': performance['worst_loss'],
        })

        return context

    def _calculate_streaks(self, matches, team):
        """Calculate current streak, longest win streak, and longest loss streak"""
        current_streak = 0
        streak_type = None  # 'win' or 'loss'
        longest_win = 0
        longest_loss = 0
        win_streak = 0
        loss_streak = 0

        # Iterate through matches from most recent to oldest
        for match in matches:
            team_won = match.winner == team

            if team_won:
                if streak_type != 'win':
                    # Switching from loss to win or starting fresh
                    longest_loss = max(longest_loss, loss_streak)
                    loss_streak = 0
                    win_streak = 0
                    streak_type = 'win'
                win_streak += 1
                current_streak = win_streak
            elif match.winner:  # Loss (match has a winner but it's not this team)
                if streak_type != 'loss':
                    # Switching from win to loss or starting fresh
                    longest_win = max(longest_win, win_streak)
                    win_streak = 0
                    loss_streak = 0
                    streak_type = 'loss'
                loss_streak += 1
                current_streak = -loss_streak  # Negative for loss streak

        # Update longest streaks one final time
        if streak_type == 'win':
            longest_win = max(longest_win, win_streak)
        elif streak_type == 'loss':
            longest_loss = max(longest_loss, loss_streak)

        return {
            'current_streak': current_streak,
            'longest_win_streak': longest_win,
            'longest_loss_streak': longest_loss,
        }

    def _calculate_performance_stats(self, matches):
        """Calculate average scores and best/worst performances"""
        if not matches:
            return {
                'avg_score': 0,
                'avg_opponent_score': 0,
                'best_win': 'N/A',
                'worst_loss': 'N/A',
            }

        total_team_score = 0
        total_opponent_score = 0
        best_win_margin = -999
        best_win_score = None
        worst_loss_margin = 999
        worst_loss_score = None

        for match in matches:
            # Add to totals
            total_team_score += match.team_score
            total_opponent_score += match.opponent_score

            # Calculate margin
            margin = match.team_score - match.opponent_score

            # Track best win (largest positive margin)
            if match.team_won and margin > best_win_margin:
                best_win_margin = margin
                best_win_score = f"{match.team_score}-{match.opponent_score}"

            # Track worst loss (largest negative margin)
            if not match.team_won and margin < worst_loss_margin:
                worst_loss_margin = margin
                worst_loss_score = f"{match.team_score}-{match.opponent_score}"

        avg_score = total_team_score / len(matches)
        avg_opponent_score = total_opponent_score / len(matches)

        return {
            'avg_score': round(avg_score, 1),
            'avg_opponent_score': round(avg_opponent_score, 1),
            'best_win': best_win_score if best_win_score else 'N/A',
            'worst_loss': worst_loss_score if worst_loss_score else 'N/A',
        }


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
            'participants', 'participants__players'
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
                queryset = queryset.filter(participants__players=player)
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
            'participants', 'participants__players'
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
            'team1', 'team2', 'location', 'match'
        ).prefetch_related(
            'team1__players', 'team2__players'
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
            'team1', 'team2', 'winner', 'location'
        ).prefetch_related(
            'team1__players', 'team2__players', 'games', 'confirmations'
        ).order_by('-date_played')
        context['completed_matches'] = completed_matches
        context['completed_count'] = completed_matches.count()

        # Pass participants count to avoid multiple COUNT queries in template
        context['participants_count'] = championship.entries.count()

        # Build results matrix for round-robin display
        # Use winner__isnull=False instead of is_confirmed=True because
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
            if private_participants:
                championship.participants.set(private_participants)
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
        # Keep the legacy M2M in step while it still exists.
        entry_player_ids = set(entry.members.values_list('player_id', flat=True))
        for team in championship.participants.prefetch_related('players'):
            if {p.pk for p in team.players.all()} == entry_player_ids:
                championship.participants.remove(team)
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
        # Use all_objects to bypass manager filtering
        return ScheduledMatch.all_objects.all()

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

from . import live_scoring  # noqa: E402  (kept local to the section it serves)


def _get_live_match_for_scorekeeper(request, pk):
    """Load a live match, enforcing scorekeeper-only access.

    Returns the Match instance. Raises PermissionDenied or Http404 on miss.
    Uses ``all_objects`` so the default manager's is_live=False filter
    doesn't hide our target.
    """
    match = get_object_or_404(Match.all_objects, pk=pk)
    try:
        user_player = request.user.player
    except (AttributeError, Player.DoesNotExist):
        raise PermissionDenied("You must have a player profile.")

    if match.scorekeeper_id != user_player.pk and not request.user.is_staff:
        raise PermissionDenied("Only the match scorekeeper can use the scoreboard.")
    return match


def _scoreboard_payload(match: Match, *, redirect_url: str | None = None) -> dict:
    """JSON-friendly payload for the scoreboard client."""
    state = match.live_state or {}
    return {
        "is_live": match.is_live,
        "is_match_complete": not match.is_live and match.live_state is None,
        "state": state,
        "current_server": live_scoring.current_server(state) if state.get("started") else None,
        "should_prompt_side_switch": (
            live_scoring.should_prompt_side_switch(state) if state.get("started") else False
        ),
        "redirect_url": redirect_url,
    }


class LiveScoreboardView(LoginRequiredMixin, View):
    """Render the scoreboard page (KAN-27 fills in the template)."""

    template_name = "pingpong/scoreboard.html"

    def get(self, request, pk):
        match = _get_live_match_for_scorekeeper(request, pk)
        if not match.is_live:
            messages.info(request, "This match is no longer live.")
            return redirect("pingpong:match_detail", pk=pk)

        cache_side_players(match)

        context = {
            "match": match,
            "team1_label": " & ".join(p.name for p in match.cached_team1_players),
            "team2_label": " & ".join(p.name for p in match.cached_team2_players),
            "bootstrap": _scoreboard_payload(match),
            "point_url": reverse("pingpong:live_point", args=[pk]),
            "start_url": reverse("pingpong:live_start", args=[pk]),
            "state_url": reverse("pingpong:live_state", args=[pk]),
            "undo_url": reverse("pingpong:live_undo", args=[pk]),
            "side_switch_url": reverse("pingpong:live_side_switch", args=[pk]),
        }
        return render(request, self.template_name, context)


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

        with transaction.atomic():
            match = _get_live_match_for_scorekeeper(request, pk)
            # Re-fetch with lock so concurrent requests serialize
            Match.all_objects.select_for_update().get(pk=match.pk)

            if not match.is_live:
                return JsonResponse({"error": "Match is not live"}, status=409)

            current = match.live_state or live_scoring.initial_state(match.best_of)
            if current.get("started"):
                # Idempotent if already started with the same server
                if current.get("initial_server") == server:
                    return JsonResponse(_scoreboard_payload(match))
                return JsonResponse({"error": "Match already started"}, status=409)

            new_state = live_scoring.set_initial_server(current, server)
            Match.all_objects.filter(pk=match.pk).update(live_state=new_state)
            match.live_state = new_state

        return JsonResponse(_scoreboard_payload(match))


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

        with transaction.atomic():
            match = _get_live_match_for_scorekeeper(request, pk)
            Match.all_objects.select_for_update().get(pk=match.pk)
            match.refresh_from_db()

            if not match.is_live:
                return JsonResponse({"error": "Match is not live"}, status=409)
            if not match.live_state or not match.live_state.get("started"):
                return JsonResponse({"error": "Match has not started — pick an initial server"}, status=409)

            try:
                new_state, completed = live_scoring.apply_point(match.live_state, side)
            except ValueError as exc:
                return JsonResponse({"error": str(exc)}, status=409)

            redirect_url = None

            if completed is None:
                Match.all_objects.filter(pk=match.pk).update(live_state=new_state)
                match.live_state = new_state
            else:
                match_complete = live_scoring.is_match_complete(new_state)
                if match_complete:
                    # Flip is_live OFF first so the upcoming Game.save() →
                    # Match.save() pipeline runs the existing winner /
                    # signal / Elo cascade as if it were a normal match.
                    Match.all_objects.filter(pk=match.pk).update(
                        is_live=False, live_state=None
                    )
                    match.is_live = False
                    match.live_state = None
                    # Hand off to the existing match_confirm flow: running
                    # the live scoreboard implicitly attests the score, so
                    # the scorekeeper is confirmed in one step and lands on
                    # match_detail. The other player still confirms via the
                    # email they receive from the post-save signal.
                    redirect_url = reverse("pingpong:match_confirm", args=[match.pk])
                else:
                    Match.all_objects.filter(pk=match.pk).update(live_state=new_state)
                    match.live_state = new_state

                # Persist the completed Game row. While is_live=True (mid-match)
                # this triggers Match.save() but the winner-detection guard
                # short-circuits. On the final game, is_live=False so the
                # normal signal pipeline runs.
                Game.objects.create(
                    match=match,
                    game_number=completed["game_number"],
                    team1_score=completed["team1_score"],
                    team2_score=completed["team2_score"],
                )

        return JsonResponse(_scoreboard_payload(match, redirect_url=redirect_url))


class LiveStateView(LoginRequiredMixin, View):
    """GET the canonical state — used by the client to rehydrate on reload."""

    def get(self, request, pk):
        # all_objects so we can return a completed match's redirect too
        match = get_object_or_404(Match.all_objects, pk=pk)
        try:
            user_player = request.user.player
        except (AttributeError, Player.DoesNotExist):
            raise PermissionDenied

        if match.scorekeeper_id != user_player.pk and not request.user.is_staff:
            raise PermissionDenied

        redirect_url = None
        if not match.is_live:
            redirect_url = reverse("pingpong:match_detail", args=[match.pk])
        return JsonResponse(_scoreboard_payload(match, redirect_url=redirect_url))


@method_decorator(require_POST, name="dispatch")
class LiveSideSwitchView(LoginRequiredMixin, View):
    """Mark the deciding-game side switch as confirmed (KAN-9)."""

    def post(self, request, pk):
        with transaction.atomic():
            match = _get_live_match_for_scorekeeper(request, pk)
            Match.all_objects.select_for_update().get(pk=match.pk)
            match.refresh_from_db()

            if not match.is_live or not match.live_state:
                return JsonResponse({"error": "Match is not live"}, status=409)

            new_state = live_scoring.confirm_side_switch(match.live_state)
            Match.all_objects.filter(pk=match.pk).update(live_state=new_state)
            match.live_state = new_state

        return JsonResponse(_scoreboard_payload(match))


@method_decorator(require_POST, name="dispatch")
class LiveUndoView(LoginRequiredMixin, View):
    """Undo the most recent point (KAN-10).

    If the undone point ended a game, the trailing Game row is deleted and
    the in-progress game state is restored from the event log.
    """

    def post(self, request, pk):
        with transaction.atomic():
            match = _get_live_match_for_scorekeeper(request, pk)
            Match.all_objects.select_for_update().get(pk=match.pk)
            match.refresh_from_db()

            if not match.is_live or not match.live_state:
                return JsonResponse({"error": "Match is not live"}, status=409)

            new_state, undone_game = live_scoring.undo_last_point(match.live_state)

            if undone_game is not None:
                # Drop the trailing Game row. Use all_objects to bypass the
                # live-match GameManager filter.
                Game.all_objects.filter(
                    match=match,
                    game_number=undone_game["game_number"],
                ).delete()

            Match.all_objects.filter(pk=match.pk).update(live_state=new_state)
            match.live_state = new_state

        return JsonResponse(_scoreboard_payload(match))

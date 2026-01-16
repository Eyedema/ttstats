import json
from typing import Any

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.core.mail import send_mail
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from .forms import GameForm, MatchEditForm, MatchForm, PlayerRegistrationForm
from .models import Game, Location, Match, Player, UserProfile, MatchConfirmation


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

    def get_queryset(self):
        return Match.objects.all().order_by("-date_played")


class MatchDetailView(LoginRequiredMixin, DetailView):
    """View to show details of a single match"""

    template_name = "pingpong/match_detail.html"
    context_object_name = "match"
    model = Match
    paginate_by = 10


class PlayerDetailView(LoginRequiredMixin, DetailView):
    """View to show details of a single player"""

    template_name = "pingpong/player_detail.html"
    context_object_name = "player"
    model = Player

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        player = self.get_object()

        # All player matches
        matches = Match.objects.filter(
            Q(team1__players=player) | Q(team2__players=player),
            match_confirmed=True
        ).select_related('team1', 'team2').prefetch_related(
            'team1__players',
            'team2__players',
            'winner'
        ).order_by('-date_played').distinct()

        total_matches = matches.count()

        # Won matches
        wins = matches.filter(
            winner__players=player
        ).count()
        losses = total_matches - wins

        # Calculate current streak
        stats = self._calculate_streaks(matches)

        context.update({
            'matches': matches,  # Limit per performance
            'total_matches': total_matches,
            'wins': wins,
            'losses': losses,
            'win_rate': (wins / total_matches * 100) if total_matches > 0 else 0,
            'current_streak': stats['current_streak'],
            'streak_type': stats['streak_type'],
            'longest_win_streak': stats['longest_win_streak'],
            'longest_loss_streak': stats['longest_loss_streak'],
        })

        return context

    def _calculate_streaks(self, matches):
        current_streak = streak_type = 0
        longest_win = longest_loss = win_streak = loss_streak = 0

        for match in matches:
            player_won = match.winner.filter(players=self.object).exists()

            if player_won:
                if streak_type != 'win':
                    longest_loss = max(longest_loss, loss_streak)
                    loss_streak = win_streak = 0
                    streak_type = 'win'
                win_streak += 1
                current_streak = win_streak
            elif match.winner.exists():  # Loss
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

        # Get statistics
        total_players = Player.objects.count()
        total_matches = Match.objects.filter(match_confirmed=True).count()
        recent_matches = Match.objects.all().order_by("-date_played")[:5]

        context.update(
            {
                "total_players": total_players,
                "total_matches": total_matches,
                "recent_matches": recent_matches,
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
                f"🎉 Match Complete! {self.match.winner} wins {self.match.team1_score}-{self.match.team2_score}!",
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
        # Redirect to match detail page after creating
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
        form.is_double = (form.cleaned_data.get('is_double') == 'True')
        player1 = form.cleaned_data["player1"]
        player2 = form.cleaned_data["player2"]
        player3 = form.cleaned_data["player3"]
        player4 = form.cleaned_data["player4"]

        if not form.is_double:
            form.player3 = None
            form.player4 = None

        # Prevent creating matches between same player
        if player1 == player2:
            messages.error(
                self.request, "You cannot create a match between the same player!"
            )
            return self.form_invalid(form)

        if form.is_double:
            if (player1 == player3 | player1 == player4) | (player2 == player3 | player2 == player4):
                messages.error(
                    self.request, "You cannot create a match between the same player!"
                )
                return self.form_invalid(form)

        # Non-staff users must be participants
        if not user.is_staff:
            try:
                user_player = user.player

                # Ensure user is one of the players
                if player1 != user_player and player2 != user_player:
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

        messages.success(self.request, "Match created successfully!")
        return super().form_valid(form)


class MatchUpdateView(LoginRequiredMixin, UpdateView):
    """View to update an existing match"""

    model = Match
    template_name = "pingpong/match_form.html"

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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        player_stats = Player.objects.annotate(
            total_matches=Count(
                'team1__matches',
                filter=Q(team1__matches__confirmations__isnull=False),
                distinct=True
            ) + Count(
                'team2__matches',
                filter=Q(team2__matches__confirmations__isnull=False),
                distinct=True
            ),
            wins=Count('matches_won'),
            total_games=Sum('team1__matches__games__id', distinct=True) +
                        Sum('team2__matches__games__id', distinct=True)
        ).select_related('user')

        for player in player_stats:
            player.losses = (player.total_matches or 0) - (player.wins or 0)
            player.win_rate = (
                    (player.wins or 0) / (player.total_matches or 1) * 100
            )

        player_stats = sorted(
            player_stats,
            key=lambda p: (p.win_rate, p.wins, p.total_matches),
            reverse=True
        )

        context["player_stats"] = player_stats
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

            # Get all matches between these players
            matches = (
                Match.objects.filter(
                    # Only matches between these two exact players
                    team1__players=player1,
                    team2__players=player2,
                    team1__players__count=1,
                    team2__players__count=1
                )
                .filter(
                    # Completamente confermati
                    confirmations__match__team1__players__isnull=False,
                    confirmations__match__team2__players__isnull=False
                )
                .distinct()
                .prefetch_related("games")
                .order_by("date_played")
            )

            if matches.exists():
                # Basic stats
                total_matches = matches.count()
                player1_match_wins = matches.filter(winner=player1).count()
                player2_match_wins = matches.filter(winner=player2).count()

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
                        if match.player1 == player1:
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

                # Recent form (last 5 matches)
                recent_matches = list(matches.order_by("-date_played")[:5])
                player1_recent_wins = sum(
                    1 for m in recent_matches if m.winner == player1
                )
                player2_recent_wins = sum(
                    1 for m in recent_matches if m.winner == player2
                )

                # Match margins (for average margin per match chart)
                match_margins = []
                for match in matches:
                    if match.winner == player1:
                        margin = (
                            match.team1_score - match.team2_score
                            if match.player1 == player1
                            else match.team2_score - match.team1_score
                        )
                    elif match.winner == player2:
                        margin = -(
                            match.team1_score - match.team2_score
                            if match.player1 == player1
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

                context.update(
                    {
                        "player1": player1,
                        "player2": player2,
                        "has_data": True,
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
                        "matches": matches.order_by("-date_played"),
                    }
                )
            else:
                context.update(
                    {
                        "player1": player1,
                        "player2": player2,
                        "has_data": False,
                    }
                )

        return context


class PlayerRegistrationView(CreateView):
    """View that creates User + Player"""

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
            from_email="pingpong@ubaldopuocci.org",
            recipient_list=[user.email],
            fail_silently=True,
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
        """Add error message on failed registration"""
        messages.error(self.request, "Please correct the errors below.")
        return super().form_invalid(form)


@login_required
def match_confirm(request, pk):
    """Allow a player to confirm a match"""
    match = get_object_or_404(Match, pk=pk)

    try:
        user_player = Player.objects.get(user=request.user)

        # Verify the player belongs to one of the two teams
        if (user_player not in match.team1.players.all() and
                user_player not in match.team2.players.all()):
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


class EmailResendVerificationView(LoginRequiredMixin, View):
    """Resend verification email"""

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

            send_mail(
                subject="Verify your email address",
                message=f"Welcome {user.username}! Click here to verify your email: {verification_url}",
                from_email="pingpong@ubaldopuocci.org",
                recipient_list=[user.email],
                fail_silently=True,
            )

            messages.success(
                request,
                f"Verification email sent! Check your inbox at {request.user.email}",
            )

        # Redirect to player profile if exists, otherwise dashboard
        if hasattr(request.user, "player"):
            return redirect("pingpong:player_detail", pk=request.user.player.pk)
        return redirect("pingpong:dashboard")


class CustomLoginView(LoginView):
    """Custom login view with tailwind styling"""

    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_success_url(self):
        return reverse_lazy("pingpong:dashboard")

    def form_valid(self, form):
        user = form.get_user()
        print(f"DEBUG: User {user.username} logging in.")
        print(
            f"DEBUG: Email verified: {getattr(user.profile, 'email_verified', 'No profile')}"
        )
        print(
            f"DEBUG: Verification token: {getattr(user.profile, 'email_verification_token', 'No profile')}"
        )
        # Check if email is verified
        if hasattr(user, "profile") and not user.profile.email_verified:
            messages.warning(
                self.request,
                "Please verify your email before logging in. Check your inbox!",
            )
            return redirect("pingpong:login")

        messages.success(self.request, f"Welcome back, {user.username}!")
        return super().form_valid(form)

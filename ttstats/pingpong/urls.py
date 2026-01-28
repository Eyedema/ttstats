from django.urls import path
from . import views

app_name = "pingpong"

urlpatterns = [
    # Auth
    path("signup/", views.PlayerRegistrationView.as_view(), name="signup"),
    path("login/", views.CustomLoginView.as_view(), name="login"),
    path("verify-email/<str:token>/", views.EmailVerifyView.as_view(), name="email_verify"),
    path("resend-verification/", views.EmailResendVerificationView.as_view(), name="email_resend_verification"),

    # Core pages
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("leaderboard/", views.LeaderboardView.as_view(), name="leaderboard"),
    path("head-to-head/", views.HeadToHeadStatsView.as_view(), name="head_to_head"),
    path("calendar/", views.CalendarView.as_view(), name="calendar"),

    # Players
    path("players/", views.PlayerListView.as_view(), name="player_list"),
    path("players/add/", views.PlayerCreateView.as_view(), name="player_add"),
    path("players/<int:pk>/", views.PlayerDetailView.as_view(), name="player_detail"),
    path("players/<int:pk>/edit/", views.PlayerUpdateView.as_view(), name="player_edit"),

    # Matches
    path("matches/", views.MatchListView.as_view(), name="match_list"),
    path("matches/add/", views.MatchCreateView.as_view(), name="match_add"),
    path("matches/<int:pk>/", views.MatchDetailView.as_view(), name="match_detail"),
    path("matches/<int:pk>/edit/", views.MatchUpdateView.as_view(), name="match_edit"),
    path(
        "matches/<int:match_pk>/add-game/",
        views.GameCreateView.as_view(),
        name="game_add",
    ),
    path("head-to-head/", views.HeadToHeadStatsView.as_view(), name="head_to_head"),
    path("signup/", views.PlayerRegistrationView.as_view(), name="signup"),
    path('match/<int:pk>/confirm/', views.match_confirm, name='match_confirm'),
    path("calendar/", views.CalendarView.as_view(), name="calendar"),
    path("matches/schedule/", views.ScheduledMatchCreateView.as_view(), name="match_schedule"),

# Passkey management
    path("passkeys/", views.PasskeyManagementView.as_view(), name="passkey_management"),
]

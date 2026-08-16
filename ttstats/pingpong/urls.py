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
    # htmx: live validation of the match form, saving nothing.
    path("matches/validate/", views.MatchValidateView.as_view(), name="match_validate"),
    path("matches/<int:pk>/", views.MatchDetailView.as_view(), name="match_detail"),
    path("matches/<int:pk>/edit/", views.MatchUpdateView.as_view(), name="match_edit"),
    path(
        "matches/<int:match_pk>/add-game/",
        views.GameCreateView.as_view(),
        name="game_add",
    ),
    path('match/<int:pk>/confirm/', views.match_confirm, name='match_confirm'),

    # Live scoreboard (KAN-4)
    path("matches/<int:pk>/live/", views.LiveScoreboardView.as_view(), name="live_scoreboard"),
    path("matches/<int:pk>/live/start/", views.LiveStartView.as_view(), name="live_start"),
    path("matches/<int:pk>/live/point/", views.LivePointView.as_view(), name="live_point"),
    path("matches/<int:pk>/live/state/", views.LiveStateView.as_view(), name="live_state"),
    path("matches/<int:pk>/live/side-switch/", views.LiveSideSwitchView.as_view(), name="live_side_switch"),
    path("matches/<int:pk>/live/undo/", views.LiveUndoView.as_view(), name="live_undo"),
    path("matches/schedule/", views.ScheduledMatchCreateView.as_view(), name="match_schedule"),
    path("scheduled-matches/<int:pk>/", views.ScheduledMatchDetailView.as_view(), name="scheduled_match_detail"),
    path("scheduled-matches/<int:scheduled_match_pk>/convert/", views.ScheduledMatchConvertView.as_view(), name="scheduled_match_convert"),
    path("scheduled-matches/<int:pk>/edit/", views.ScheduledMatchEditView.as_view(), name="scheduled_match_edit"),

    # Passkey management
    path("passkeys/", views.PasskeyManagementView.as_view(), name="passkey_management"),

    path('championships/', views.ChampionshipListView.as_view(), name='championship_list'),
    path('championships/create/', views.ChampionshipCreateView.as_view(), name='championship_create'),
    # htmx fragment: the participant picker, re-rendered when the type changes.
    path(
        'championships/participants-fragment/',
        views.ChampionshipParticipantsFragmentView.as_view(),
        name='championship_participants_fragment',
    ),
    path('championships/<int:pk>/', views.ChampionshipDetailView.as_view(), name='championship_detail'),
    path('championships/<int:pk>/edit/', views.ChampionshipEditView.as_view(), name='championship_edit'),
    path('championships/<int:pk>/register/', views.ChampionshipRegisterView.as_view(), name='championship_register'),
    path('championships/<int:pk>/unregister/', views.ChampionshipUnregisterView.as_view(), name='championship_unregister'),
    path('championships/<int:pk>/start/', views.ChampionshipStartView.as_view(), name='championship_start'),
]

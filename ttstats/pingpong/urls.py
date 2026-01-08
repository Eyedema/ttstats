from django.urls import path
from . import views

app_name = "pingpong"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("leaderboard/", views.LeaderboardView.as_view(), name="leaderboard"),
    path("players/", views.PlayerListView.as_view(), name="player_list"),
    path("players/add/", views.PlayerCreateView.as_view(), name="player_add"),
    path("players/<int:pk>/", views.PlayerDetailView.as_view(), name="player_detail"),
    path(
        "players/<int:pk>/edit/", views.PlayerUpdateView.as_view(), name="player_edit"
    ),
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
]

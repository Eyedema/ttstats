# The `all_objects` / `_base_manager` audit (KAN-24)


| Site | Why it bypasses | Resolution |
|---|---|---|
| `views.LeaderboardView` | Whole-club table, not "matches I played in" | **Scoped.** Highest risk: it feeds the cached payload |
| `views._get_live_match_for_scorekeeper` | `is_live=False` default filter hides the target | **Scoped** |
| `views.LiveStateView` | Must return a completed match's redirect | **Scoped** |
| `views.ScheduledMatchConvertView` | Organiser converts a match they don't play in | **Scoped** |
| `views.ScheduledMatchEditView` | Same | **Scoped** |
| `context_processors.pingpong_context` | Badge counting only your own championships is not a badge | **Scoped** |
| `ChampionshipDetailView` (4 sites) | Participants see all championship matches | **Left club-wide** — filtered by `championship=`, and the championship itself came through the club-scoped manager. Transitively safe |
| `MatchManager` / `ScheduledMatchManager` `_scope_to_user` | `Championship.all_objects` inside an `Exists()` subquery | **Left club-wide** — correlated to `OuterRef('championship_id')` on an already-scoped row |
| `models.Match.recompute`, `services` | Writes to a known pk | **Left club-wide** — no reads |
| `achievements.py`, `notifications.py` | Background/system paths with no viewer | **Left club-wide** — `get_current_user()` is None, the unfiltered contract |
| `Season.for_day` (KAN-46) | Resolves the season of a match the caller already holds | **Left club-wide** — filtered by the match's `club_id`; a write must use the match's club, not the viewer's |
| `elo.LiveBook` (KAN-47) | `PlayerRating.all_objects` for the match's own players | **Left club-wide** — filtered by the participants of a match the caller holds; a write must not be hidden by the viewer's club |
| `ratings.club_seasons` (KAN-48) | `Season.all_objects` for the leaderboard's season choices | **Scoped** — filtered by the explicit active club; `Season.objects` is unscoped with `clubs` off |
| `ratings.leaderboard`, experience subquery (KAN-50) | `PlayerRating.all_objects` all-time count | **Left club-wide** — correlated to `OuterRef("player")` of a row read through the club-scoped `PlayerRating.objects` |
| `ratings.leaderboard`, unrated list (KAN-48) | `PlayerRating.all_objects` inside an `Exists()` | **Left club-wide** — correlated to `OuterRef("pk")` of the club-scoped `Player.objects`, and filtered to the season's club |
| `ratings.player_format_ratings` (KAN-50) | `PlayerRating.all_objects` for one player's rows | **Left club-wide** — filtered by a player the caller read through the club-scoped manager |
| `season_rollover` (KAN-51) | `Season`/`PlayerRating`/`SeasonStanding.all_objects`, `Player._base_manager` | **Left club-wide** — a scheduled job, no viewer; every read is bounded by a season or a club id |
| `Season.clean` overlap check (KAN-46) | `Season.all_objects` for the clash query and the stored dates | **Left club-wide** — filtered by the season's own `club_id` / pk; the check must see seasons the viewer's club cannot |
| `SeasonAdmin.save_model` warning (KAN-9) | `Match.all_objects` count of unrated confirmed matches | **Scoped** — filtered by the saved season's `club_id` explicitly; the admin has no active club |
| `rating_replay`, `recalculate_ratings` (formerly `elo_replay`, `elo_parity`, `recalculate_elo`, migration 0040) | `_base_manager` replay of the whole history | **Left club-wide** — commands and migrations, no viewer |
| `seed_demo`, `seed_e2e`, `award_achievements`, `backfill_clubs` | Management commands | **Left club-wide** — same |


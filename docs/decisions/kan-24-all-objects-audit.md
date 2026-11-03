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
| `elo_replay`, `elo_parity`, `recalculate_elo`, migration 0040 | `_base_manager` replay of the whole history | **Left club-wide** — commands and migrations, no viewer |
| `seed_demo`, `seed_e2e`, `award_achievements`, `backfill_clubs` | Management commands | **Left club-wide** — same |


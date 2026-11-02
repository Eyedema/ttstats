---
paths:
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/signals.py"
  - "ttstats/pingpong/services.py"
  - "ttstats/pingpong/achievements.py"
  - "ttstats/pingpong/elo.py"
  - "ttstats/pingpong/match_state.py"
  - "ttstats/pingpong/notifications.py"
  - "ttstats/pingpong/tests/test_match_save_performance.py"
---

# The match-completion write path

Saving the winning Game fans out through three `post_save` receivers, the Elo
pipeline and eleven achievement checkers. Its query count must stay **flat in
the players' match history**: `tests/test_match_save_performance.py` pins both
a ceiling and the slope, and the slope is the one that matters.

## Memos, and their invalidation contract

- **`Match.sides()`** returns both sides as lists from one memoized query (or
  the `participants` prefetch). `players_on()` is still a queryset and costs a
  round trip per call. **`all_players` is a list.**
- **`Match._confirmation_state()`** answers verified-side-1, verified-side-2 and
  confirmed-ids in one annotated query. The
  `player__user__profile__email_verified` LEFT JOIN yields NULL for a player with
  no account, which must keep reading as "not verified".
- **Anything that rewrites participant or confirmation rows must invalidate**:
  `services.set_match_sides` calls `invalidate_participant_cache()`, the
  `MatchConfirmation` signal calls `invalidate_confirmation_state()`, and
  `refresh_from_db()` drops both. A stale memo is worse than the queries saved.
- **`Match.from_db` records the persisted `winner_side`**, so
  `track_match_winner_change` does not re-read the row (a re-read through the
  scoped manager raises `DoesNotExist` for a match the viewer can't see and
  skips the pipeline). `save()` updates the recorded value afterwards.

## Achievements

- **`achievements.PlayerContext` is built once per player** and passed to every
  checker. Checkers take `(player, match, ctx=None)` and build their own when
  called directly.
- `match.games.count()` and `.order_by()` bypass the prefetch cache. Use
  `len(match.games.all())` and rely on `Game.Meta.ordering`.

## Result notification

- `match_state.py` holds the pure decisions: `winner_side()`,
  `side_confirmed()`, `confirmation_complete()`, `should_auto_confirm()`.
- **`Match.result_notified_at` makes the result push exactly-once**:
  `notify_match_confirmed` claims the match with a conditional `.update()`.
  Never set the field by hand.

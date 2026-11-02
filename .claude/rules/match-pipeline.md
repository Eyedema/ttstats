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

### The match-completion write path is memoized, and memos must be invalidated

Saving the winning Game of a match fans out through three `post_save`
receivers, the Elo pipeline and eleven achievement checkers. It measured
**114 queries** for a two-player match and grew **+2 for every match already in
the players' history** -- a cost that rose forever with how much they had
played. It is now **39, and flat** (36 at 100 matches of history).
`tests/test_match_save_performance.py` pins both the ceiling and the *slope*;
the slope is the one that matters, because a ceiling that holds at 60 matches
and not at 600 is not a ceiling.

Four things carry that, and three of them are caches with an invalidation
contract:

- **`Match.sides()`** returns both sides as lists from one query, memoized, and
  reads the prefetch cache when `participants` is already loaded. `players_on()`
  is still a queryset for callers that need one -- but it is another round trip
  per call, and the pipeline asked seven times per save. **`all_players` is now a
  list**, off the same read; every caller iterates it and none used a queryset
  method.
- **`Match._confirmation_state()`** answers verified-side-1, verified-side-2 and
  confirmed-ids in a **single** annotated query over the participant rows, where
  there used to be three. `match_confirmed` alone cost three queries and is read
  about eight times per save. The `player__user__profile__email_verified` LEFT
  JOIN yields NULL for a player with no account -- which must keep reading as
  "not verified", or two unverified players could never confirm a match.
- **Anything that rewrites participant or confirmation rows must invalidate.**
  `services.set_match_sides` calls `invalidate_participant_cache()`; the
  `MatchConfirmation` signal calls `invalidate_confirmation_state()`;
  `refresh_from_db()` drops both. A memo that outlives the rows it describes is
  worse than the queries it saved -- the pipeline would decide against a stale
  picture of who still owes an answer.
- **`Match.from_db` records the persisted `winner_side`**, so
  `track_match_winner_change` no longer re-reads the row on every save. That was
  also a latent bug, not just a query: the re-read went through the **scoped**
  manager, so a match invisible to the current viewer raised `DoesNotExist` and
  silently skipped the whole completion pipeline. `save()` updates the recorded
  value afterwards, so a second `save()` of the same instance still reports the
  winner as already set.

**`achievements.PlayerContext` is built once per player** and handed to every
checker. Each checker previously resolved its own inputs -- the player's whole
history was loaded five separate times, and `check_rivalry` resolved each
historical opponent with its own query, which was the entire linear term.
Checkers still take `(player, match, ctx=None)` and build their own context when
called directly, which is how the per-checker tests stayed unchanged. Two
things there are easy to undo by accident: `match.games.count()` and
`.order_by()` both bypass the prefetch cache and re-query -- use
`len(match.games.all())`, and rely on `Game.Meta.ordering`.

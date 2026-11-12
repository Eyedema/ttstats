# Per-scorer op streams (KAN-149, KAN-158, KAN-159)

First story of KAN-148 (shared live scoring). The rules are in
`.claude/rules/live-scoring.md`; this records the choices behind them.

## One code path: host rows for live matches (option (b))

Migration `0049_live_scorer` gives every match that is live at deploy time a
host `LiveScorer` row (the scorekeeper, on their side; side 1 for a
scorekeeper who is not playing), with `applied_seq = live_applied_seq or 0`
and `can_score_both = True`. Every new live match gets one from
`services.create_live_match`. So every match being scored goes through the
scorer-row path.

The **row-less read** survives for two shapes only: a match that finished
before the deploy (a retried match point must still be answered with the
completion) and fixtures built straight from `MatchFactory`. It is what the
ticket calls the legacy shape: stream `Match.live_applied_seq`, only scorer
`match.scorekeeper`. It is a read fallback, not a second write path.

`live_applied_seq` is still written by the **host** stream for one release,
so a rolled-back deploy finds the stream where the old code reads it. A
guest's ops never touch it (two streams in one column is meaningless).
Reversing the migration copies the higher of the host row and the column back
into the column, then drops the table.

## `can_score_both` is True for a solo host

KAN-150 rejects a point for the other side when `can_score_both` is False.
A solo scorer scores both sides, so the solo host row (MatchCreateView, the
migration) is created with True; the join flow (KAN-152) passes False to
`create_live_match` and adds the guest row itself.

## `rev` does not move on a rule rejection

`Match.live_rev` is +1 per op that changed the state, from any stream. A rule
rejection consumes its seq (KAN-77) but changes nothing anyone renders, so
it does not bump `rev`: a poller (KAN-168) woken by it would only re-fetch
an identical state, and KAN-150's harness can assert `rev` = applied ops.
The rejecting phone learns its new `applied_seq` from its own response. A
legacy op without `seq` that applies does bump it.

`rev` is written in the same queryset `.update()` as `live_state` and kept in
memory too, because the game-ending `Game.save()` saves the whole match
instance.

## The scorer row is written once per request

`_consume` advances the scorer's `applied_seq` **in memory**; `save_scorer`
writes it, together with a due `last_seen_at`, in one UPDATE before the
op's transaction commits. Per-op writes would have added one UPDATE per op
(~40 on the pinned 40-point batch, past the 230 ceiling); the transaction is
the same, so a failure still rolls the seq back with the op. The pinned
ceiling is unchanged, and `test_live_scorers.py::TestQueryCost` pins it for a
match with a host row.

## Authorization

`_get_live_match_for_scorer` (scoped `all_objects` + scorer prefetch) accepts
a non-revoked row, the scorekeeper of a row-less match, or staff. It runs
before the lock as a cheap gate, and again on the rows re-read under the
lock, so a revocation that lands while a request waits is honoured.

Staff without a row: viewing is allowed; on a solo match they write through
the one active stream, exactly as before; on a shared match a write is 409
`not a scorer` (an unattributed third writer is undebuggable). Presence and
`me` are only ever the caller's own row, so staff never mark the host seen.

An op without `seq` in a shared match is 409 `reload`: it cannot be deduped
against two streams, and only a pre-KAN-77 cached page sends one.

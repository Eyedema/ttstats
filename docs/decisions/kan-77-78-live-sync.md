# Idempotent live ops and the batch sync endpoint (KAN-77, KAN-78)

Server half of KAN-76 (offline-proof live scoring). The rules are in
`.claude/rules/live-scoring.md`; this records the choices behind them.

## A rejected op consumes its seq

The tickets fix duplicates (`seq <= applied`) and gaps (`seq > applied + 1`)
but not what a rule rejection does to `applied`. **It consumes the seq.** The
client drops or flags a rejected op (S7.5) and keeps the numbers it already
gave the ops behind it; if the seq were not consumed, every one of those would
be a gap and the outbox would have to renumber itself, which is exactly the
kind of client logic that goes wrong offline. A rule rejection is
deterministic, so re-sending the same op could never succeed anyway.

An op refused because the match is **not live** does not consume its seq:
`live_applied_seq` keeps naming the last op that changed anything, which is
what a client reconciling after completion needs.

A consequence: a re-send of a rejected op is answered as a duplicate (200,
canonical payload), not with the rejection again. The payload is the truth,
so a client rendering from it shows the right score either way.

## The legacy start idempotency survives for unsequenced requests only

`LiveStartView` answered a repeated start with the same server with 200. A
sequenced re-send is now a duplicate and goes through the generic check; the
special case is kept only for requests with no `seq`, because "no seq" is
promised to behave exactly as before while a cached page is still out there.
It goes when the legacy path does.

## Gaps inside a batch

A batch whose first new op is past `applied + 1` is 409 `gap`, exactly like a
single op. A batch with a hole in the middle applies up to the hole and says
so in `rejected` (`"error": "gap"`) with a 200, because something was applied
and the client must learn the new `applied_seq`. Either way the client resends
from `applied_seq + 1`.

## A malformed batch applies nothing

Partial success is for *rule* rejections. A malformed op (bad type, missing
side, duplicate seq inside the batch) is a client bug, so the whole batch is
validated before the lock is taken and refused with 400.

## Query cost

A 40-point batch (two game ends) is 213 queries, about five per point: the
point write's savepoint and release, the `MAX(sequence)`, the insert and the
state `UPDATE`. That is linear in the batch and flat in the length of the
log, which is the property that matters on the VPS; `test_live_sync.py` pins
it at 230. Writing `live_state` once per batch instead of once per op would
save ~40 queries but needs a second code path through `live_ops`; not worth
it until a measurement says otherwise.

## `client_recorded_at`

`PointEvent.client_recorded_at` is the op's `client_ts`, clamped to
`[match day - 1 day, now]` (UTC), null when absent or unparseable -- a bad
clock never costs a point. `created_at` stays the server's time. **Momentum
and duration features should prefer `client_recorded_at` when present**: for
a point synced after a dead spell, `created_at` is when the network came
back, not when the point was played. No existing consumer was changed.

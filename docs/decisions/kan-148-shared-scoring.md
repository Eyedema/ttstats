# Shared live scoring (KAN-148)

Two phones scoring one match. The epic's later stories append their sections
here. The per-scorer op streams (KAN-149) have their own record,
`docs/decisions/kan-149-per-scorer-streams.md`; the rules are in
`.claude/rules/live-scoring.md`.

## KAN-150: multi-writer op rules

### Side restriction

A scorer row with `can_score_both = False` may only score for its own `side`.
Anything else is 409 `not your side`, a rule rejection: it consumes the seq
(the phone drops the op and carries on) and does not bump `rev`. A solo host
has `can_score_both = True` (KAN-149), and a row-less legacy match has no
scorer to restrict, so one-phone scoring is unaffected. The check runs before
"not started", so a wrong-side tap is named as such even before the start.

### Attribution

`PointEvent.recorded_by` (FK Player, `SET_NULL`) is set on every new point,
single-scorer matches included, so the column means something from now on:
the player of the stream that applied it (`live_ops.recorded_by`), or the
scorekeeper of a row-less match. Staff driving a solo match write through
the host's stream, so their points are the host's. Rows written before the
field existed stay null and are **never backfilled**: a guess would be
indistinguishable from a record.

The same id goes on the `live_state` event as `by`, so the state carries
attribution without a query. `live_scoring` treats it as an opaque value
(`apply_point(..., by=)`), the JS mirror passes it through the same way, and
the golden vectors gained an appended `attributed` scenario; every earlier
sequence kept its bytes. The client's reconcile compares events by side,
game and game end only, so a server event with `by` and a predicted event
without one still agree.

### Conditional undo, and who may undo

Undo deletes the trailing `PointEvent`. With two phones, "the trailing point"
is a moving target: A sees its own point #7 at the end, taps Undo, and B's
point #8 lands first. An unconditional undo then deletes B's point and keeps
A's mistake.

So an undo may carry `expects`, the sequence the client believes is trailing
(it learns sequences from the payload's `last_point: {sequence, side, by}`).
If the trailing row's sequence differs, or there is nothing to undo, the undo
is 409 `stale undo` (consumes the seq, deletes nothing). The client re-reads
the state and asks again. In a **shared** match an undo without `expects` is
refused the same way; in a single-scorer match it stays optional, so today's
client is unaffected.

**Either scorer may undo the trailing point, whoever recorded it.** The two
phones belong to the two players at the same table, and "that point was
wrong" is as often noticed by the opponent as by whoever tapped it.
Restricting undo to the recorder would force the wrong phone to act and add a
round trip to every correction. `expects` is what makes the open rule safe:
nobody can delete a point they have not seen. `by` on the event is there so a
later UI can say *whose* point an undo would remove, not to gate it.

### Start and side switch

- Two `start`s: the first to arrive wins; the second is `ALREADY_STARTED`
  (existing behaviour, consumes its seq). The losing phone rebases onto the
  server's `initial_server` (client work: S13.5).
- Two `side_switch`es: `confirm_side_switch` sets a flag, so the second is a
  no-op on the state. Both are applied ops (each bumps `rev`); nothing else
  changes. Pinned in `test_live_multiwriter.py`.

### Arrival order: the game-boundary hazard (documented, not fixed)

Points are applied in **arrival order** under the match lock. The server
cannot know the order in which rallies were actually played: two phones tap
within the same second and the network decides. Inside a game this is mostly
harmless: swapping two adjacent points of different sides changes the
intermediate scores stored on the two rows (and so streak and comeback
statistics) but not the game's final score, and the server attribution
depends only on how many points have been played.

**At a game boundary it changes who won the game.** Worked example, game 1,
side 1 leads 10-9, side 1 served first:

| Real rallies | Phone | Tap |
|---|---|---|
| rally 20: side 2 wins (10-10, deuce) | B (side 2) | point team2 |
| rally 21: side 1 wins (11-10) | A (side 1) | point team1 |

B's request is slow (a weak signal), A's arrives first:

| Arrival | Applied as | Row |
|---|---|---|
| A: team1 | 11-9: **game 1 ends, side 1 wins it** | #20, game 1, 11-9 |
| B: team2 | first point of game 2: 0-1 | #21, game 2, 0-1 |

The truth is game 1 at 11-10, still in progress. The server's record is
plausible, a `Game` row exists, and both phones show the server's state. B's
phone predicted 10-10 and now sees "game 2, 0-1": the reconcile flags a
mismatch (server wins, toast + report), which is the only signal anyone gets.
The fix is a human one: undo #21 (`expects: 21`), undo #20 (`expects: 20`,
which deletes the game row), and re-tap in the right order. Each undo needs
`expects` precisely because the other phone may be tapping meanwhile.

No reordering in this story. Options for later: the client could hold a tap
that would end a game for a short grace window, the scoreboard could ask for
confirmation of a game point when another phone tapped within N ms, or the
server could reject a game-ending point when the other stream's
`last_seen_at` shows it mid-flight. Each costs latency on every game point;
the decision waits for real two-phone usage.

### The fidelity harness for two streams

`tests/test_point_fidelity.py::TestTwoStreams` generates a points script,
splits it by side into stream A (side 1) and stream B (side 2), and delivers
it in a seeded interleaving that keeps each stream's order, through the
single-op endpoints (with re-sends) and through `live_sync` (one batch mode
per seed: 1-op, random chunks, maximal runs, overlapping re-sends). Batches
never span the other stream's ops, so the arrival order is the script's.
Undos with a correct `expects`, undos with a stale one, the second `start`,
both `side_switch`es and wrong-side taps are mixed in.

The oracle is `live_scoring` replaying the same arrival order: the rows
(side, game, server, scores) and `recorded_by` must match, the match must
complete at most once, and `rev` must equal the number of applied ops. A
single-stream replay through the endpoints (one solo host, the same arrival
order) must leave the same rows apart from `recorded_by`. Two canaries prove
it fails against a writer that drops `recorded_by` and an undo that ignores
`expects`.

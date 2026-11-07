# Offline-proof live scoring (KAN-76)

The venue has slow, unreliable 4G. Before this epic the scoreboard was
optimistic but online-only: a failed POST lost the point, a lost response
plus a retry would have double-counted it, and the page could not open with
no signal. After it, a whole match can be scored with no connectivity and
syncs exactly once, in order, when the network returns.

This is the umbrella record. Each part has its own, with the details:

| Part | Record |
|---|---|
| Server: client `seq` + dedupe, `live_ops`, `live_sync`, `client_recorded_at` (KAN-77/78) | `kan-77-78-live-sync.md` |
| Client: outbox, flush loop, plain-JS scoreboard, sync UI (KAN-80/81) | `kan-80-81-offline-client.md` |
| Service worker vs. the e2e suite, and the offline proxy (KAN-96, KAN-82) | `kan-76-sw-e2e-audit.md` |

## The shape

1. **An op log with client sequence numbers.** Every scorekeeper action
   (`start`, `point`, `undo`, `side_switch`) is an op with a per-match,
   gapless, client-assigned `seq`.
2. **The server dedupes by `seq`** (`Match.live_applied_seq`, its own column
   because `live_state` is nulled at match end and the most dangerous
   duplicate is a retried match point). Every retry is therefore safe.
3. **One batch endpoint** (`live_sync`) applies a backlog under one lock, op
   by op through the same `live_ops.apply_op` a single tap uses.
4. **The client runs the rules locally** with `live_rules.js`, a mirror of
   `live_scoring.py` verified by golden vectors generated from the Python.
5. **A durable outbox** (localStorage, keyed by match and user) holds every
   op before anything is sent.
6. **A narrow service-worker cache** opens the scoreboard (and only the
   scoreboard) with no signal.

## Why the server stays the authority

The JS engine exists so the screen can move with no network, not to decide
anything. The server re-applies every op through `live_scoring.py`; when its
answer differs from the client's prediction, the server wins, the client
re-derives (server state + remaining ops) and reports the divergence. A
second authority would need conflict resolution; a verified mirror needs
only a parity test (`npm run test:js`), which fails if either side drifts.

## Why the service-worker cache is limited to the scoreboard

A stale leaderboard or player page is a lie with no way to notice it. The
scoreboard is different: it carries `applied_seq` and rebuilds from the
newer of its bootstrap and the outbox's last acknowledged snapshot plus the
unsynced ops, so a stale copy still shows the right score. Everything else
(every POST, all JSON, every other page) is not intercepted. Reading other
pages offline is KAN-131's job, and it must label what it shows.

## Explicitly left out

- **Creating a match offline.** The match row must exist; starting and
  scoring it work offline.
- **Merging two devices scoring the same match.** A conflict is detected
  (a gap that doesn't resolve, or `applied_seq` past anything this device
  issued) and this device switches to read-only follow mode, dropping its
  unsynced ops.
- **Offline confirmation, ratings, achievements.** They run server-side when
  the completing op syncs, exactly as before.

## Verification

- pytest: `test_live_seq.py`, `test_live_sync.py`, `test_client_recorded_at.py`,
  `test_point_fidelity.py` (duplicates and four batch splits, each with a
  test proving the harness fails against a broken server), `test_offline.py`.
- `npm run test:js`: rules parity over 512 generated sequences, outbox and
  session unit tests against a fake `live_sync`.
- e2e chaos specs (`scoreboard-offline-cases.js`, WebKit and desktop Chrome
  under `applyProdCSP`): full match offline, lost ack, slow 4G with loss,
  session expiry, server rejection. `scoreboard-offline.pwa.spec.js`
  (`iphone-pwa`, through `offline-proxy.js`): reload with no signal, lie-fi,
  queued taps surviving an offline reload and syncing once, nothing else
  served offline, eviction and identity purges.
- **Still owed:** one real iPhone in airplane mode against production
  (KAN-84), recorded on the ticket.

## Follow-ups

- Remove the legacy no-`seq` path of the four single-op endpoints once no
  cached page can send it (logged at INFO as "without seq").
- `live_sync` writes `live_state` once per op; once per batch would save
  about 40 queries on a 40-point batch.
- A page opened before the worker controlled it (the very first visit) is
  not stored; the page could ask the worker to fetch and store itself.

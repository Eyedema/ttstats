# The offline-first scoreboard client (KAN-80, KAN-81, KAN-83)

Client half of KAN-76. The rules are in `.claude/rules/live-scoring.md`
("Scoreboard client"); this records the choices behind them.

## localStorage, not IndexedDB, for the outbox

The epic left it open. **localStorage**, one key per (match pk, user id):

- **Durability at the tap.** `localStorage.setItem` is synchronous: when the
  tap handler returns, the op is on disk. An IndexedDB write commits
  asynchronously, so a tab killed by iOS between the tap and the commit loses
  the op -- the exact failure the outbox exists to prevent.
- **Volume.** A best-of-5 is a few hundred ops of ~80 bytes; the 5 MB quota is
  three orders of magnitude away.
- **No wrapper.** IndexedDB would need a promise wrapper (or a library) and a
  migration story; the outbox is 250 lines without one.
- The cost: Safari may evict script-writable storage after seven days without
  a visit, and IndexedDB is not exempt from that either. A match is scored and
  synced the same day. `navigator.storage.persist()` is requested anyway,
  best effort.

Storage that throws (private mode, quota) leaves a working in-memory outbox
and a reported warning; corrupt or foreign records are discarded and
reported, never thrown into a tap handler.

## The page is plain JS, not Alpine

The old scoreboard was an Alpine component and only worked in production
because prod's CSP allows `'unsafe-eval'`. The e2e `PROD_CSP` withholds it on
purpose, so the old page could never be tested the way it ships, and KAN-80
requires the new one to be green under `applyProdCSP`. All logic is now in
static files (`live_outbox.js`, `live_session.js`, `scoreboard.js`), which is
also what makes it unit-testable. Every overlay starts with the `hidden`
attribute; a `#scoreboard [hidden]{display:none!important}` rule is inlined in
the page because Tailwind's preflight `[hidden]` loses to any display utility.

## A stale page rebuilds on the newest base

The service worker (KAN-82) serves the scoreboard network-first, so offline
it may serve an old HTML with an old bootstrap. The outbox keeps the last
acknowledged server payload (`snapshot`); `LiveSession` starts from whichever
of bootstrap and snapshot has the higher `applied_seq`, drops ops at or below
it, and replays the rest. Without the snapshot, a stale bootstrap plus the
pending ops would skip every op acknowledged since the page was cached.

## Conflicts are detected, not merged

- **Divergence** (server state != the mirror's prediction at the same seq):
  server wins, toast, report. Compared on scores, games, game number, server,
  side switch and the event sides -- never timestamps.
- **Another device**: the server's `applied_seq` is past any seq this phone
  issued, so our ops at those seqs were taken as duplicates. The tab goes
  read-only ("follow mode", polling `LiveStateView` every 5 s) and offers
  "Score on this phone instead", which drops the outbox.
- **A 409 gap** means ops the server needs are no longer in this outbox. One
  retry (a race), then follow mode.

## Reports

There is no client-side Sentry (`build-and-deploy.md`: Sentry is server-side
only). `scoreboard.js` reports to `console.warn`, to
`window.ttstatsReport(message, extra)` if anything defines it, and as a
`ttstats:report` event: the hook a client SDK would plug into.

## Signing in again returns to the scoreboard

`CustomLoginView.get_success_url` ignored `next`, so "Sign in again to save
the score" would have landed on Today with the ops still waiting. It now
honours a safe `next` (Django's `get_redirect_url` checks the host) and falls
back to Today.

## Proving the chaos specs

Each scenario in `scoreboard-offline-cases.js` was run against the pre-epic
scoreboard (its template and inline Alpine, with `data-score`/`data-side`
aliases added so the selectors resolve, and under the e2e server's real
endpoints). Results are in the KAN-83 commit message.

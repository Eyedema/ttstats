# Shared-scoring lobby and join flow (KAN-151, KAN-152)

Part of KAN-148. Rules: `.claude/rules/clubs.md` (the join link) and
`.claude/rules/live-scoring.md` (the lobby). This records the choices.

## An unverified new account is logged in to join

`PlayerRegistrationView` does not log a new account in, and `CustomLoginView`
refuses an unverified one. The ticket says not to block a joiner on email
verification (unverified players already play; their matches auto-confirm).
Without a session the joiner cannot reach the scoreboard, so the hook
(`consume_pending_live_join`) logs the account in **only** when the session
holds a valid, open lobby token and the account is a member of the lobby's
club (via a club invite consumed in the same signup). The verification email
is still sent, and the next login still requires it. Any other signup is
unchanged.

## Login returns through `live/join/`, not `?next=<join URL>`

The ticket asks for a safe `next` and also that the token appear in no URL
but the join path. Both hold: the token is parked in the session, and the
login `next` is the token-free `live/join/` resume route, which redirects to
the join URL. "Not you? Log out" posts `next` in the form body.

## The lobby views live in their own module

`live_lobby_views.py`, like `season_views.py`: views.py is 4,000 lines and
three KAN-148 stories edit it in parallel. views.py only gains the
registration hook call and `MatchCreateView.get_initial` (the
"Pick opponent instead" prefill).

## Staff cannot open another host's lobby

The scoped manager lets staff see every lobby in their club; the lobby page
additionally filters on `host=<viewer>`, because the page shows the bearer
token.

## The join rate limit covers GET and POST

The invite accept limits GET only; a POST to the join URL is the same oracle,
so both count against `20/h` by IP. A legitimate join is about four requests.

## "Waiting for an opponent" on Today (KAN-157)

- **The host opts in, default on.** The card widens who can join from "whoever
  holds the code" to "any member of the club", so the match form has a
  "Let anyone in the club join" checkbox, rendered checked
  (`LiveLobby.open_to_club`, default True). An unchecked box posts nothing,
  so a missing field reads as off (fail closed); the lobby page says which
  it is. Default on because at the club both players are usually signed in
  already, which is the whole point of the card.
- **A token-free route, `live/lobby/<pk>/join/`,** reached only from the
  card. The lookup is `LiveLobby.all_objects` + `scope_to_active_club` +
  `open_to_club=True` (KAN-24 audit), so another club's lobby or a private
  one is a 404 whoever asks (staff included). It renders the same
  `live_join.html` and posts through the same `start_shared_match`; every
  identity branch (own, ended, rejoin, lost race) is `_resolve_join`, shared
  with the token link. The confirm form and "Not you?" point at the pk
  route, so the token never reaches a non-host's page. No rate limit: a pk
  is not a credential, and the route only shows what Today already lists.
- **Not cached, not amber, not refreshed.** `DashboardView._waiting_lobbies`
  queries on every load (Today has no fragment cache; only "Lately" is in
  the cache and it is unaffected), so no invalidation is needed. Gated on
  the `shared_scoring` flag like the other entry points; the route itself is
  not, like the token link.
- Newest first, at most 3 (`TODAY_LOBBY_LIMIT`), never the viewer's own.

## Not done here

- A "Score live with a shared link" entry on Today: Today has no "Score live"
  entry (only the resume banner), so there is nothing to sit next to.
- Pruning old lobby rows (optional in the ticket): rows are tiny and expiry is
  lazy; add a `run_scheduled_jobs` job if the table ever matters.
- `LiveStateView` answered a finished match with `match_detail`, while
  `live_sync` answered `match_confirm`. Since fixed by KAN-153: both answer
  `match_confirm` (`kan-148-shared-scoring.md`).

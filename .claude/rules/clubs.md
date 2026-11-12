---
paths:
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/templates/pingpong/_nav_menu.html"
  - "ttstats/pingpong/templates/pingpong/club_*.html"
  - "ttstats/pingpong/tests/test_club*.py"
  - "tests/e2e/club-*.spec.js"
  - "ttstats/pingpong/live_lobby_views.py"
  - "ttstats/pingpong/templates/pingpong/live_join.html"
  - "ttstats/pingpong/tests/test_shared_scoring.py"
  - "ttstats/ttstats/sentry.py"
---

# Clubs: switcher, invites, roles

## The club switcher has no hidden state

It lives in `_nav_menu.html`, rendered by both the drawer and the desktop sidebar.

- **In the drawer, never the tab bar.** Tabs are destinations; a club is a context.
- **No dropdown.** A list of plain POST forms, never collapsed -- it cannot
  render open-and-unusable and works with scripting off (the fail-closed rule).
- **POST, not a link**, or any page could switch someone's club with an `<img>`.
  `club_id` is coerced to int before the ORM, or a malformed POST is a 500.
- **Absent for single-club users**, but the club **name** still renders in the
  mobile header and sidebar kicker.
- The active row is marked with `aria-current`, not colour alone.
- **Switching lands on Today**; staying put would 404 on the old club's page.
- A live match in the club you leave keeps running; it is keyed on its own pk.
- `get_user_memberships()` answers which clubs, which is active and what role
  from **one** memoized query. Don't split it back into three.

## Club invites are bearer credentials

`ClubInvite` + `/pingpong/join/<token>/`. The token *is* the authorization.

- Token is `uuid.uuid4().hex` (same as `email_verification_token`), compared with
  `secrets.compare_digest`, never logged. The accept endpoint is rate limited
  (`20/h` by IP).
- **Registration is hooked, never forked**: `PlayerRegistrationView.form_valid`
  calls `consume_pending_invite()`.
- **`accept_club_invite` takes an explicit `user`** -- at registration the new
  account is not logged in yet, so `request.user` is anonymous.
- **The token is parked in the session, not the signup URL**; the form posts to
  itself and a query param is lost on the first validation error.
- **`claim()` is a conditional `.update()`** so two simultaneous opens of a
  single-use link cannot both win. Never check `invite.is_used_up` in Python.
- **An existing member's visit does not burn a use.**
- **Acceptance guarantees a Player exists** (`base.html` needs `user.player.pk`).
- **A dead link gets a real page** with the specific reason (expired / revoked /
  used up). Never a 500.
- **The generated link is shown once**, on the redirect after creation.
- New token-bearing routes must be added to `TOKEN_PATH_PREFIXES` in
  `ttstats/sentry.py` in the same commit.

## A shared-scoring join link is a bearer credential too

`LiveLobby` + `/pingpong/live/join/<token>/` (KAN-152, `live_lobby_views.py`)
follows every rule above: `uuid4().hex`, `all_objects` lookup by token then
`compare_digest`, `20/h` by IP (GET and POST), parked in the session
(`pending_live_join`), registration hooked (`consume_pending_live_join`, after
`consume_pending_invite`), a real page per dead reason, `live/join` in
`TOKEN_PATH_PREFIXES`.

- **Login comes back via `live/join/` (resume), never `?next=<join URL>`**, so
  the token is in no URL but its own. The token is shown only on the host's
  lobby page (pinned to the host even for staff) and only while open.
- **Membership of the lobby's club is checked before joining**, and only then
  `set_active_club`; the match gets `club=lobby.club` explicitly.
- **Signup never logs in, a join link included**: with a parked, open lobby
  in a club the new account is in, the token stays parked and the "verify
  your email" page says the match is waiting. Verifying in this browser
  (`EmailVerifyView`) or a login without its own `next` (`CustomLoginView`)
  resumes via `pending_live_join_url` -> `live/join/`. An unverified session
  could create auto-confirming matches against verified clubmates and
  register a passkey, bypassing verification for good.
- The claim is a conditional `.update()` (`start_shared_match`), like `claim()`.
- **The Today card's join is not a credential path** (KAN-157):
  `live/lobby/<pk>/join/` finds the lobby through `scope_to_active_club` and
  only if the host left `open_to_club` on; it shares `_resolve_join` and
  `start_shared_match` with the link and never renders the token.

## Club roles

`ClubMembership.role` is owner / admin / member.

- **A role is per membership, never per user.**
- **Every check goes through `Club.user_can_administer()` / `user_is_owner()` /
  `role_for()` / `membership_for()`.** Never an inline `is_staff` comparison.
- **Django staff get no standing inside a club**; a member staff account is
  governed by its role like anyone else.
- **A club can never be left ownerless**: the last owner cannot be demoted or
  removed, including by themselves.
- **You cannot act on an equal or a superior** (`ClubMembership.outranks`), and
  only an owner may appoint another owner.
- `ClubAdminRequiredMixin` raises `PermissionDenied` (403), never a redirect. No
  active club at all gives 404.
- **Templates don't render controls the viewer cannot use.** `ClubSettingsView`
  computes `actionable_ids` with the same rules as the POST handlers.
- The backfill granted existing users `admin` (they could already create
  championships) and one `owner`. Later joiners get the model default.

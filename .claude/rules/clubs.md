---
paths:
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/templates/pingpong/_nav_menu.html"
  - "ttstats/pingpong/templates/pingpong/club_*.html"
  - "ttstats/pingpong/tests/test_club*.py"
  - "tests/e2e/club-*.spec.js"
---

# Clubs: switcher, invites, roles

### The club switcher has no hidden state

The switcher lives in `_nav_menu.html` -- **edited once**, rendered by both the
drawer and the desktop sidebar, which is why they cannot diverge again.

- **Placement is the drawer, never the tab bar.** The four tabs are
  *destinations*; a club is a *context*. A fifth tab would also break the rule
  that Play is a tab because it is the only thing that starts something.
- **There is deliberately NO dropdown.** The fail-closed rule applies to
  anything whose hidden state is managed by JavaScript, and the cheapest way to
  obey it is to have no hidden state: this is a list of plain POST forms, two
  or three rows long, never collapsed. It cannot render open-and-unusable, it
  needs no Alpine, no `[x-cloak]` and no compiled CSS to be correct, and it
  works with scripting off. A collapsible control here would add a failure mode
  to save one row of vertical space.
- **POST, not a link.** Switching changes what every later page shows, so a GET
  would let any page on the internet move someone's club with an `<img>` tag.
  `club_id` is coerced to int before it reaches the ORM -- a non-numeric pk
  raises out of `get_prep_value` and would turn a malformed POST into a 500.
- **Absent for single-club users**, who are almost everyone. A chooser offered
  to someone with one choice is clutter -- but the club **name** still renders,
  in the mobile header and the sidebar kicker, because "which club am I looking
  at" has to be answerable on every screen without opening the drawer.
- The active row is marked with `aria-current`, not colour alone.
- **Switching lands on Today.** Staying put would leave the viewer on a detail
  page belonging to the club they just left, which then 404s.
- **A live match in the club you switch away from keeps running.** Switching is
  a viewing change; the match is server state keyed on its own pk and the
  scorekeeper reaches it by URL. It is waiting when they switch back.
- `get_user_memberships()` answers **which clubs, which is active, and what
  role** from one memoized query. Asking separately put three queries on every
  page in the app -- one of them purely to decide whether to draw a single menu
  row. The match-list query ceiling moved 14 -> 15 for this, deliberately.

### Club invites are bearer credentials

`ClubInvite` + `/pingpong/join/<token>/` is how a second group onboards itself
with no admin creating accounts and no shell. The token *is* the authorization,
which sets every rule below.

- **The token follows the `UserProfile.email_verification_token` precedent** --
  `uuid.uuid4().hex`, already working in production. Compared with
  `secrets.compare_digest`, never logged, and the accept endpoint is rate
  limited (`20/h` by IP) because it is the only oracle for "is this token real".
- **Registration is hooked, never forked.** `PlayerRegistrationView.form_valid`
  calls `consume_pending_invite()`. A second signup path would drift from the
  one that already creates the Player, sends verification and handles the
  form's error states.
- **`accept_club_invite` takes an explicit `user`.** At registration time the
  new account exists but is *not logged in yet*, so `request.user` is still
  anonymous and the invite would silently do nothing.
- **The token is parked in the session, not on the signup URL.** The
  registration form posts to itself, so a query parameter is lost on the first
  validation error and the invite evaporates.
- **`claim()` is a conditional `.update()`**, so two people opening the same
  single-use link at the same moment cannot both win. `if invite.is_used_up:`
  in Python loses that race every time. Same shape as
  `Match.result_notified_at`.
- **An existing member's visit does not burn a use** -- otherwise a curious
  member destroys a single-use link meant for someone else.
- **Acceptance guarantees a Player exists.** `base.html` renders
  `user.player.pk` unconditionally, so a member without one 500s every page.
- **A dead link gets a real page**, with the *specific* reason (expired /
  revoked / used up). Never a 500, never silent membership. The distinction is
  not sensitive: the reader already holds the token.
- **The generated link is shown once**, on the redirect after creation, and is
  not re-rendered on later visits. A settings page that always displays every
  live link is a settings page you cannot show anyone.

### Club roles

`ClubMembership.role` is owner / admin / member. Before it, `user.is_staff` was
the app's only privilege distinction -- and staff is *global*, so it cannot
express "runs our club".

- **A role is per membership, never per user.** Someone can own one club and be
  an ordinary member of another. Conflating them is how a person ends up
  administering a club they merely play in.
- **Every check goes through `Club.user_can_administer()` / `user_is_owner()` /
  `role_for()` / `membership_for()`** -- on the model, following
  `Player.user_can_edit` and `PlayerManager.editable_by`. Never an inline
  `is_staff` comparison in a view; the copy that gets missed is the one that
  matters.
- **Django staff get no standing inside a club.** A staff account that is not a
  member cannot administer it, and one that *is* a member is governed by its
  role like anyone else. Same reasoning as the S1.4 decision. A staff member
  who needs to run a club joins it.
- **A club can never be left ownerless.** The last owner cannot be demoted or
  removed, including by themselves -- it is the one mistake with no in-app
  recovery.
- **You cannot act on an equal or a superior** (`ClubMembership.outranks`), and
  only an owner may appoint another owner. Otherwise the owner role is
  decorative: an admin would simply promote themselves.
- `ClubAdminRequiredMixin` raises `PermissionDenied` (403), never a redirect --
  a forged POST is refused, not bounced to a page that looks like success. A
  viewer with no active club at all gets 404, matching the app's existing
  "invisible things 404, they do not 403" rule.
- **Templates must not render controls the viewer cannot use.** `ClubSettingsView`
  computes `actionable_ids` with the same rules the POST handlers enforce, so
  the two cannot disagree. The 403 is a correctness backstop, not the UX.
- **The backfill grants existing users `admin`, not `member`, and appoints one
  `owner`.** That looks generous and is the conservative choice: before roles
  existed every verified user could create and start championships, so `member`
  would silently strip capability from everyone at cutover. Users who join
  *later* get the model default. The generosity is a migration artefact, not
  the policy.

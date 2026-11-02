---
paths:
  - "ttstats/pingpong/templates/**"
  - "ttstats/pingpong/assets/**"
  - "ttstats/pingpong/static/**"
  - "ttstats/pingpong/templatetags/**"
  - "ttstats/pingpong/forms.py"
  - "ttstats/pingpong/player_hues.py"
  - "ttstats/pingpong/context_processors.py"
  - "tailwind.config.js"
  - "tests/e2e/**"
---

# Frontend: templates, design system, htmx, CSP

### What the overhauled screens promise

- **Today shows only what can need you.** "Waiting on you" excludes matches you
  have already confirmed -- a match waiting on the *other* player is not
  waiting on you, and listing it under that heading is how a dashboard teaches
  people to stop reading the block. `total_players` / `total_matches` are gone
  on purpose: neither is actionable and neither changes between two visits.
- **`elo.projected_elo_changes()` is the number the user is asked to agree to**
  ("-16 Elo if true" on Today, "+14 Elo if agreed" on match detail) *and* the
  number `update_player_elo` writes -- it returns the K-factors too, so even
  the recorded `EloHistory.k_factor` comes from the same call. Never compute a
  preview separately; a figure that turns out different is the app lying about
  a decision it asked the user to make.
- **Rivalries are ordered by recency, not volume.** A rivalry is live because
  it is ongoing. Only confirmed matches count -- an unconfirmed result is a
  claim, not a record.
- **The leaderboard's weekly movement is derived in both cache paths.** The
  page is cached per filter set; `_presentation_context()` exists so the hit
  and the miss cannot produce different context, and so the htmx fragment gets
  everything it renders.
- **Movement is an arrow PLUS a signed number**, and zero is an em dash rather
  than absence. Colour is never the only signal anywhere in this design.
- **Match detail resolves the viewer's position once**, in `_viewer_context`.
  The old template asked `{% if user.player in match.side1_players %}` in four
  duplicated places and they had come to disagree about whether an
  already-confirmed side still sees a Confirm button.

### The design system

The app was overhauled onto a single palette and type scale. The rules below
are the ones that break something invisible if ignored.

- **Colour resolves through CSS custom properties**, not literal hexes.
  `tailwind.config.js` maps every colour to `rgb(var(--token) / <alpha-value>)`
  and `app.css` declares the triples. **Dark is the base and light is the
  override**, because the viewer's system setting drives it. Only *surfaces*
  flip; paddle red, ball amber, confirmed green and the eight player hues are
  identities and hold the same value in both themes.
- **Three colours carry meaning and never drift.** Red is the action you can
  take. **Amber means live right now and appears nowhere else** -- not on
  warnings, not on "pending". Green means confirmed, i.e. agreed by both sides.
- The **semantic aliases** (`background`, `card`, `muted`, `primary`, ...) are
  kept and remapped rather than replaced, which is why the screens the overhaul
  did not re-lay-out still re-skin correctly. Do not reintroduce literal
  palette classes: `bg-white` is white-on-white on the navy court, and a
  hardcoded `blue-100` chip cannot flip with the theme.
- **Radius is 0 everywhere, `rounded-full` included.** The one exception is the
  live ball, which gets its circle from `.dot` in app.css rather than from a
  utility, so the table in the config can stay absolute.
- **Player hues** come from `player_hues.py` (pure, no Django), keyed on the
  player's pk so a hue survives a rename and two people never swap. Templates
  use `{{ player|hue }}` (or `player.hue_class`) plus `.hue-bar` / `.hue-fill` /
  `.hue-text`; hue never goes into an inline `style`.
- **Type comes from named specimens** (`text-display`, `text-title`,
  `text-heading`, `text-score-hero`, `.label-cap`, `.n`) which carry size,
  leading, tracking and weight together. There is no such thing as a 34px
  display in a different weight.
- **Archivo is self-hosted** (OFL, vendored from `@fontsource/archivo` by
  `npm run vendor` into `static/pingpong/fonts`). Prod's CSP allows no external
  hosts, so a Google Fonts link would silently not load and the whole design
  would fall back to system-ui. The Dockerfile copies the fonts, and prod's
  manifest storage resolves the `url()`s at collectstatic time -- a missing
  font file fails the deploy.

### Icons are inlined symbols, not `<img>`

`{% icon "name" %}` (templatetags/icon_tags.py) emits `<use href="#i-name">`
against the sprite in `pingpong/_icons.html`, included once at the top of
`<body>`.

- **`<img src=...svg>` cannot work here.** Every Lucide file declares
  `stroke="currentColor"`, and an `<img>` is its own document with no inherited
  colour -- so every icon resolved to black. Survivable on white, invisible on
  navy, and it made an amber or red icon impossible.
- **The sprite is generated but COMMITTED** (`npm run icons`). It is a template
  partial, not a build artefact: templates must render from a fresh clone with
  no npm step.
- **A symbol that is not in the sheet renders an empty box, silently** -- no
  error, no failed request. `tests/test_navigation.py::TestIconSprite` is the
  guard, and it covers both `{% icon "literal" %}` call sites and the
  data-driven achievement glyphs, which cannot be found by scanning templates.
- Any page that does not extend `base.html` must include the sprite itself.
  `registration/base_auth.html` does, because `_messages.html` renders icons.

### The navigation spine

Nine identical sidebar links became four tab destinations -- **Today, Play,
Table, Cups** -- plus a tiered drawer.

- **Play is a tab because it is the only thing that starts something**, and it
  is deliberately absent from the drawer menu: it is a button, not a place you
  browse to.
- The active tab is resolved by `context_processors.TAB_FOR_URL_NAME` and the
  `{% nav_active %}` tag. **It is a tag rather than an inline comparison
  because `{% include with %}` cannot evaluate one**: `active=nav_tab == 'today'`
  passes the truthy *string* and every row lights up at once, with no error.
- A destination that is not a tab (Calendar, Head to head, Everyone, All
  matches) highlights nothing. Claiming a tab the user did not tap is worse
  than showing no selection.
- The drawer and the desktop sidebar render **the same `_nav_menu.html`**. They
  used to be two hand-maintained copies and had already diverged.
- `pb-tabbar` on `<main>` and the bar's own height both come from the
  `spacing.tabbar` token, so the padding and the bar cannot disagree about how
  tall the bar is and hide the last row of a list.

### htmx Fragments
Two shapes, both in use:
- **Dual-mode view** -- one URL serves the page and the fragment. `LeaderboardView` does this:
  `is_htmx(request)` (views.py) reads the `HX-Request` header, and `get_template_names()` picks
  the partial. Prefer this over branching inside `get_context_data`.
- **Dedicated fragment view** -- its own URL, only ever returns a partial:
  `ChampionshipParticipantsFragmentView`, `MatchValidateView`.
- **The full page must `{% include %}` the same partial the fragment returns**, so the two cannot
  drift. All three partials (`_leaderboard_results`, `_championship_participants`, `_form_errors`)
  are included on first paint.
- **A fragment swapped with `hx-swap="outerHTML"` must render its own target `id`.** Drop it and
  htmx can never target that element again -- the first swap works and every later one silently
  does nothing.
- Use `hx-push-url="true"` where the old code did a full submit, or you quietly remove deep links
  and the back button.
- Tom Select hides the real `<select>`, so its change events don't reach an ancestor's
  `hx-trigger`. Re-dispatch a bubbling `change` from its `onChange`.
- `<body>` carries `hx-headers` with the CSRF token; per-element wiring isn't needed.

### Forms
- **Never hand-write `<option>` loops.** Render the widget (`{{ form.field }}`) or include `pingpong/_field.html`. A hand-written loop comparing `form.f.value` to a literal is wrong on a bound form: the POST value is a *string*, so `"7" == 7` is false and the user's choice disappears when validation fails.
- `_field.html` renders label, widget, help text and **every** error (templates used to print `.errors.0` and drop the rest). Optional context: `label`, `help`, `badge`, `wrapper_class`.
- `StyledFormMixin` (forms.py) puts `.field-input` on every widget and sets `error_css_class`. Add it to any new form; do not paste Tailwind strings into Python.
- **Adding classes in a view:** use `append_widget_class(field, css)`. `widget.attrs.update({"class": ...})` replaces the attribute and silently drops `field-input`.
- Choice lists belong in forms.py (`BEST_OF_CHOICES`), not in template markup.
- **Live validation** is `MatchValidateView` (`matches/validate/`), which binds the real form and
  renders `_form_errors.html`. It saves nothing. Validation rules belong there and in
  `Form.clean()` -- never re-implemented in JS.
- Filtering "still unfilled" errors must key on Django's `required` **error code**, not the
  message: "Four players are required for a doubles match!" is a rule violation that happens to
  contain the word "required".

### Template / Frontend
- **Never `{% url 'pingpong:player_detail' <player>.pk %}` directly.** Use
  `{% player_link player css="..." %}` (`templatetags/player_tags.py`), which degrades to plain
  text when the player is gone. `MatchParticipant.player` is `on_delete=CASCADE`, so deleting a
  player empties a side of every match they played; the raw `{% url %}` then gets an empty pk and
  raises `NoReverseMatch`, permanently 500-ing the match list, match detail and player detail for
  everyone. This happened.
- `base.html` unconditionally renders `{% url 'pingpong:player_detail' user.player.pk %}` — every authenticated user **must** have a linked Player profile
- Use `json_script` template tag for passing data to JavaScript, NOT `escapejs` (causes double-serialization)
- Chart.js colors: use explicit `rgb()` values (e.g., `rgb(59, 130, 246)`), not CSS custom properties (render as black)
- **The flash auto-dismiss in `base.html` keys on `[data-flash]`, not `[role="alert"]`.** It sweeps
  the DOM 5s after load, so the old selector deleted any long-lived alert on the page — the
  scoreboard's error toast — whether or not it had ever been shown. `_messages.html` sets the
  attribute; a new flash partial must too.
- `[x-cloak]` is declared once in `app.css`. Alpine is deferred, so every `x-show` element needs it
  or it flashes visible on first paint. Don't re-add a per-template `<style>` copy.

### Production sends a CSP; dev does not

**Dev sends no CSP at all.** That asymmetry, not any particular directive, is
the thing to remember: the mobile drawer once shipped broken to production
while pytest, a manual browser pass and the whole Playwright suite were green,
because none of them had ever seen the header the real server sends.

**`prod.py`'s `script-src` is currently `'self' 'unsafe-inline' 'unsafe-eval'`.**
The `'unsafe-eval'` is deliberate and load-bearing: Alpine 3's standard build
compiles every expression with `new Function()`, so without it `x-data`,
`x-show`, `x-text` and `@click` all throw, while Alpine still gets far enough
to strip `x-cloak` -- overlays render open with dead dismiss controls. That is
the failure mode above.

- **`scoreboard.html` is Alpine and works in production *because* of that
  allowance.** It is not broken; do not "fix" it. (This section previously
  said the opposite, and stayed wrong after `'unsafe-eval'` was added.)
- **The mobile drawer is deliberately plain JS.** Do not "modernise" it back
  onto Alpine, even though eval is now permitted. The e2e suite's `PROD_CSP`
  is deliberately **stricter** than what prod sends -- it has no
  `'unsafe-eval'` -- precisely so the drawer, the one overlay that is
  unrecoverable when it fails open, can never quietly regress onto a
  framework expression.
- Removing **both** unsafe directives means the `@alpinejs/csp` build (every
  expression becomes an `Alpine.data()` member) alongside B.5's removal of
  inline `<script>` blocks. Until then `'unsafe-inline'` already permits
  injected inline scripts, so the policy's XSS value is limited either way.
- `tests/e2e/helpers.js` exports `applyProdCSP(page)`; `csp.spec.js` asserts no
  script is blocked. Any spec covering interactive behaviour should apply it.
- **`iphone-dark` is a Playwright project, and it matters.** Dark is the
  palette's base, so the light rendering a developer sees on a desktop is the
  *variant*. `palette.dark.spec.js` asserts the media query actually reached
  the page before asserting anything else -- without that check the whole file
  would pass in light mode for entirely the wrong reason.
- **Keep `PROD_CSP` in sync when adding a directive.** It also carries
  `worker-src` and `manifest-src` for the service worker and web manifest.

### Perceived speed: the app was silent, not slow

The installed PWA has no browser chrome — no throbber, no URL bar, no native
progress line — so a tap with no immediate visual change is indistinguishable
from a tap that never registered. Measured server think-time is **15–140ms**
(dashboard worst at ~110–140ms, leaderboard ~15ms) with 4–29 queries per page
and no N+1 anywhere. The app was never slow; it just said nothing.

- **HTML is gzipped by `GZipMiddleware`, placed directly below WhiteNoise.**
  Below, deliberately: WhiteNoise short-circuits static requests in
  `process_request` and serves its own pre-compressed siblings, so a static hit
  never reaches GZip and no CPU is spent re-gzipping a woff2. Everything that
  does reach it is a rendered template. Pages are ~40% whitespace, ~42KB of
  Tailwind class attributes, and every one inlines the 20KB icon sprite — the
  match list was **143KB on the wire and is 12.6KB compressed** (80–91% across
  the app). BREACH is not a concern: Django re-masks the CSRF token per
  response, so there is no stable plaintext to correlate.
- **Touch feedback is declared once in `@layer base`, against element
  selectors** (`a[href]`, `button`, `summary`, `[role=button]`, `label[for]`,
  submit/button inputs) plus an opt-in `.pressable`. It is deliberately *not* a
  class sprinkled over the ~100 call sites: the failure mode of that approach
  is a control someone forgot, and a control that does not respond is exactly
  the bug being fixed.
- **Opacity is the press signal, not colour.** It composites over whatever
  background the element already has — paddle red, bone, a player hue — so no
  per-variant rule is needed, and it survives reduced motion, which narrows
  `transition-property` to exactly this kind of property and drops the scale.
  Two signals when motion is allowed, one when it is not.
- `.pressable-flat` opts out of the scale only. The tab bar and drawer rows use
  it: scaling a 56px tab makes the whole fixed bar look like it is wobbling.
- **`hover:` cannot do this job.** On iOS it latches after the tap and leaves
  the element you just left lit up.
- `touch-action: manipulation` removes the ~300ms double-tap-zoom delay, which
  on its own is longer than any response this app produces.
- **`#nav-progress` is fail-closed like the drawer**: it renders `scaleX(0)`,
  `opacity:0`, `pointer-events:none`, and only the inline plain-JS handler adds
  `.is-loading`. It is fixed across the top of the viewport where the mobile
  header's controls are, so `pointer-events:none` is load-bearing — without it
  it becomes an invisible tap-blocking strip. It animates toward 90% and never
  reaches it (it cannot promise a completion time it does not know); arrival is
  the next document replacing it. `pageshow`/`pagehide` clear it, or bfcache
  restores a document that looks like it is loading forever.
- **Testing it needs care.** Playwright queues every evaluation in a frame
  behind that frame's pending navigation, so a *delayed* route makes the
  assertion run against the new document and it fails whether or not the
  feature works. `tap-feedback.spec.js` aborts the navigation instead, keeping
  the armed document alive. The synthetic-click variant registers its
  `preventDefault` listener *after* the page's own, since the page's handler
  bails on `e.defaultPrevented`.

### `from:` in an hx-trigger takes a CSS selector, not a jQuery one

`change from:#date_filter[value!='custom']` shipped on the leaderboard. `!=` is
not a CSS attribute operator, so the selector threw, htmx never bound that
trigger, and **changing Period did nothing at all** while Format and Show
worked — a dead control, which is the loudest possible version of "did my tap
register?". It also raised a `pageerror` on every leaderboard load.

The exclusion was unnecessary: picking "Custom range" with no dates leaves
`filter_start_date` None in `LeaderboardView`, so the request returns all-time
results and reveals the date inputs, which is what choosing a custom range
should do. Expressing it as an htmx event filter would have needed
`new Function()` — i.e. `'unsafe-eval'`, which the e2e `PROD_CSP` deliberately
withholds.

### Fail-closed rule for JS-managed UI

**UI whose hidden state is managed by JavaScript must fail closed.** A drawer
shipped that was permanently open and unclosable on iOS Safari: its "closed"
state depended on Alpine booting *and* on the compiled CSS carrying
`[x-cloak]`, and its close button was also Alpine. Two things had to go right
or the user was trapped. The version it replaced used a static `hidden` class
and needed zero.

- `[x-cloak]{display:none !important}` and `#mobile-menu-scrim.hidden{display:none}`
  are **inlined in `base.html`'s `<head>`** as well as coming from `app.css`, so
  they cannot be lost to a bad build, a purge, or a failed fetch. Do not remove
  the inline copies as duplicates — the drawer is the one element that is
  unrecoverable when it renders by mistake, since it covers the viewport and
  its dismiss control is inside it.
- Every new overlay/drawer/modal needs a spec asserting it stays closed when
  Alpine and/or the stylesheet fail to load (`tests/e2e/mobile-drawer.spec.js`).
- **pytest cannot see any of this.** It asserts on the rendered template
  string; the string was perfect while the page was broken. Claims about what
  is on screen need `npm run test:e2e`. See the `frontend-verification` skill.

### Motion, materials & user preferences
- **The `prefers-reduced-motion` / `prefers-reduced-transparency` blocks in `app.css` sit outside
  every `@layer`** so they beat both component classes and Tailwind utilities. Moving them into a
  layer silently disarms them.
- Reduced motion narrows `transition-property` to opacity/colour rather than zeroing durations:
  colour feedback still carries meaning, and elements that declared no transition don't acquire
  one. Animations are clamped to a single 1ms iteration, which also parks `animate-pulse` on its
  final frame instead of looping.
- `.chrome-blur` + `.chrome-edge-bottom` are the translucent nav surfaces (sidebar, mobile header).
  The edge is a gradient pseudo-element on the *fixed* element, not a `border-b`. Any new frosted
  surface must also be covered by the reduced-transparency block, which goes fully solid — a
  half-transparent fallback is the worst case for legibility.
- The mobile menu is Alpine (`menuOpen` on the `.flex.min-h-screen` root, `@keydown.escape.window`).
  It enters from the left and leaves the same way; keep the enter/leave transforms mirrored.

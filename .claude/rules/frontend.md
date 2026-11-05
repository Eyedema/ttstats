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
  - "scripts/perf.mjs"
  - "scripts/e2e_server.sh"
---

# Frontend: templates, design system, htmx, CSP

How to verify any of this in a real browser: the `frontend-verification` skill.

## The design system

- **Colour resolves through CSS custom properties.** `tailwind.config.js` maps
  every colour to `rgb(var(--token) / <alpha-value>)`; `app.css` declares the
  triples. **Dark is the base, light is the override.** Only surfaces flip;
  paddle red, ball amber, confirmed green and the eight player hues don't.
- Red = the action you can take. **Amber = live right now, nowhere else** (not
  warnings, not "pending"). Green = confirmed by both sides.
- Semantic aliases (`background`, `card`, `muted`, `primary`, ...) are remapped,
  not replaced. Don't reintroduce literal palette classes (`bg-white`,
  `blue-100`): they can't flip with the theme.
- **Radius is 0 everywhere, `rounded-full` included.** The live ball gets its
  circle from `.dot` in app.css.
- **Player hues** come from `player_hues.py`, keyed on pk. Use
  `{{ player|hue }}` / `player.hue_class` plus `.hue-bar` / `.hue-fill` /
  `.hue-text`; never an inline `style`.
- **Type comes from named specimens** (`text-display`, `text-title`,
  `text-heading`, `text-score-hero`, `.label-cap`, `.n`).
- **Archivo is self-hosted** (`npm run vendor`). Prod's CSP allows no external
  hosts, so a Google Fonts link would silently fall back to system-ui.
- **Movement is an arrow PLUS a signed number**; zero is an em dash. Colour is
  never the only signal.

## Icons are inlined symbols, not `<img>`

`{% icon "name" %}` emits `<use href="#i-name">` against the sprite in
`pingpong/_icons.html`, included once at the top of `<body>`.

- `<img src=...svg>` can't inherit `currentColor`, so icons render black.
- **The sprite is generated but COMMITTED** (`npm run icons`) so templates render
  from a fresh clone.
- **A symbol missing from the sheet renders an empty box, silently.**
  `tests/test_navigation.py::TestIconSprite` covers literal call sites and the
  data-driven achievement glyphs.
- A page not extending `base.html` must include the sprite itself
  (`registration/base_auth.html` does).

## The navigation spine

Four tabs -- **Today, Play, Table, Cups** -- plus a tiered drawer.

- **Play is a tab because it is the only thing that starts something**; it is
  absent from the drawer.
- The active tab comes from `context_processors.TAB_FOR_URL_NAME` and the
  `{% nav_active %}` tag. `{% include with active=nav_tab == 'today' %}` passes
  a truthy *string* and lights every row.
- Non-tab destinations (Calendar, Head to head, Everyone, All matches)
  highlight nothing.
- The drawer and desktop sidebar render **the same `_nav_menu.html`**.
- `pb-tabbar` on `<main>` and the bar's height both use `spacing.tabbar`.

## What the overhauled screens promise

- **Today shows only what can need you.** "Waiting on you" excludes matches you
  already confirmed.
- **Rivalries are ordered by recency, not volume**, and count confirmed matches only.
- **Leaderboard weekly movement is derived in both cache paths** via
  `_presentation_context()`, so hit, miss and htmx fragment agree.
- **Match detail resolves the viewer's position once**, in `_viewer_context`
  (and the rating scope its figures are in).
- **The Table** (`_leaderboard_season_results.html`) filters by season and
  format through a rendered `LeaderboardScopeForm` with
  `hx-trigger="change, submit"` (no `from:` selector to break).

## Rating components (KAN-71)

- Show the number plus a muted `±n` and sr-only "give or take n"
  (`_rating_value.html`); uncertainty is text, never opacity or colour alone.
- Every rating component carries `data-rating-system`. No "Elo" copy anywhere
  (Elo was retired by KAN-73); `tests/test_glicko_ui.py` sweeps the rendered pages.
- Chart.js bands use two datasets with `fill`, the player hue (`player.hue_hex`),
  axis colours read with `getComputedStyle` and passed as literal `rgb()`.
- E2E: `e2e-glicko` user (`glicko.json`); centre a link before tapping it when a
  fixed tab bar could cover it (`scrollIntoView({block:'center'})`).

## htmx

- **Dual-mode view** (one URL, page or fragment): `is_htmx(request)` plus
  `get_template_names()`, as in `LeaderboardView`. **Dedicated fragment view**:
  `ChampionshipParticipantsFragmentView`, `MatchValidateView`.
- **The full page `{% include %}`s the same partial the fragment returns.**
- **A fragment swapped with `hx-swap="outerHTML"` must render its own target
  `id`**, or every swap after the first silently does nothing.
- Use `hx-push-url="true"` where a full submit used to be.
- Tom Select hides the real `<select>`; re-dispatch a bubbling `change` from its
  `onChange`.
- `<body>` carries `hx-headers` with the CSRF token.
- **`from:` in `hx-trigger` takes a CSS selector.** `[value!='custom']` threw and
  killed the leaderboard's Period control. Event filters need `new Function()`,
  which the e2e `PROD_CSP` withholds.

## Forms

- `_field.html` renders label, widget, help text and **every** error. Optional
  context: `label`, `help`, `badge`, `wrapper_class`.
- `StyledFormMixin` puts `.field-input` on every widget; add it to new forms.
  Don't paste Tailwind strings into Python.
- **In a view, use `append_widget_class(field, css)`**; `widget.attrs.update(
  {"class": ...})` drops `field-input`.
- Choice lists live in forms.py (`BEST_OF_CHOICES`).
- **Live validation is `MatchValidateView`**, which binds the real form and
  renders `_form_errors.html`. Never re-implement rules in JS.
- Filter "still unfilled" errors on the `required` **error code**, not message text.

## Templates

- `MatchParticipant.player` is `on_delete=CASCADE`; a raw `{% url %}` on an empty
  side raises `NoReverseMatch` and 500s the page for everyone. Hence `player_link`.
- **Flash auto-dismiss keys on `[data-flash]`, not `[role="alert"]`.** A new
  flash partial must set it.
- `[x-cloak]` is declared in `app.css` (and inlined in `base.html`). Every
  `x-show` element needs it. Don't re-add per-template copies.

## CSP

- **Dev sends no CSP.** Prod's `script-src` is `'self' 'unsafe-inline'
  'unsafe-eval'`. `'unsafe-eval'` is load-bearing for Alpine 3, which compiles
  expressions with `new Function()`.
- **The mobile drawer is deliberately plain JS.** Don't move it to Alpine. The
  e2e `PROD_CSP` omits `'unsafe-eval'` on purpose so the drawer can't regress.
- Removing both unsafe directives needs `@alpinejs/csp` plus removal of inline
  `<script>` blocks (B.5).
- Keep `PROD_CSP` in `tests/e2e/helpers.js` in sync with `prod.py`, including
  `worker-src` and `manifest-src`.

## Fail-closed rule for JS-managed UI

- `[x-cloak]{display:none !important}` and `#mobile-menu-scrim.hidden{display:none}`
  are **inlined in `base.html`'s `<head>`** as well as in `app.css`. They are not
  duplicates to remove.
- Every new overlay/drawer/modal needs a spec asserting it stays closed when
  Alpine and/or the stylesheet fail to load (`tests/e2e/mobile-drawer.spec.js`).

## Touch feedback and perceived speed

The installed PWA has no browser chrome; a tap with no visual change reads as a
tap that never registered.

- **Touch feedback is declared once in `@layer base` against element selectors**
  (`a[href]`, `button`, `summary`, `[role=button]`, `label[for]`, submit/button
  inputs) plus opt-in `.pressable`. Not a class sprinkled over call sites.
- **Opacity is the press signal**, not colour; it survives reduced motion.
- `.pressable-flat` drops the scale (tab bar, drawer rows).
- **Don't use `hover:` for this**; it latches on iOS.
- `touch-action: manipulation` removes the double-tap-zoom delay.
- **`#nav-progress` is fail-closed**: `scaleX(0)`, `opacity:0`,
  `pointer-events:none`; only the inline plain-JS handler adds `.is-loading`.
  `pointer-events:none` is load-bearing (it sits over the header controls).
  `pageshow`/`pagehide` clear it for bfcache.
- `GZipMiddleware` sits directly below WhiteNoise so static hits never reach it.

## Motion and materials

- **The `prefers-reduced-motion` / `prefers-reduced-transparency` blocks in
  `app.css` sit outside every `@layer`.** Moving them into a layer disarms them.
- Reduced motion narrows `transition-property` to opacity/colour and clamps
  animations to one 1ms iteration.
- `.chrome-blur` + `.chrome-edge-bottom` are the translucent nav surfaces. Any
  new frosted surface must be covered by the reduced-transparency block.
- The mobile drawer enters and leaves from the left; keep the transforms mirrored.

## Page weight and speed (KAN-119)

- **The e2e server serves static the way prod does** (`settings/e2e.py`:
  manifest + gzip storage, WhiteNoise, `runserver --nostatic`), so a template
  naming a file missing from the manifest 500s in e2e as it would in prod.
- **Byte budgets live in `tests/e2e/budgets.spec.js`** (WebKit, cold cache,
  compressed bytes from `request.sizes()`). A story that makes a page lighter
  tightens its row in the same commit; raising one needs a stated reason.
- **`npm run perf`** (Chromium, CDP-throttled, median of 5, cold + warm) writes
  `perf-report.md`; baseline and after-numbers go in
  `docs/decisions/kan-119-performance.md`. The key-page list is shared:
  `tests/e2e/perf-pages.js`.
- **No parser-blocking script in `<head>`.** htmx and Alpine are both `defer`,
  htmx first (defer keeps document order; Alpine's init must see htmx).
  Nothing may touch `window.htmx` at parse time: wrap it in
  `DOMContentLoaded` or listen for `htmx:*` events on `document`/`body`.
  Guarded by `test_navigation.py::TestScriptLoading` and
  `tests/e2e/script-loading.spec.js`.
- **Static is pre-compressed as `.br` and `.gz`** (`brotli` installed, so
  WhiteNoise writes both); HTML stays on `GZipMiddleware` (Brotli measured
  ~9% smaller on the dashboard, ~1 KB: not worth a custom middleware).

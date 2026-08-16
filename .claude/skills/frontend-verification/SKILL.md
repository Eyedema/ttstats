---
name: frontend-verification
description: How to actually verify a TTStats frontend change before shipping it. Use whenever a change touches templates, app.css, Tailwind config, Alpine/htmx behaviour, or anything whose effect is visual, mobile-only, or dependent on JavaScript running. Covers the Playwright/WebKit suite, what pytest cannot see, and the fail-closed rule.
---

# Verifying a frontend change

## Why this exists

A mobile navigation drawer once shipped that was permanently open and
impossible to close on iOS Safari. Every check that ran was green:

- 714 pytest tests passed — they assert on the **rendered template string**,
  and the string was perfect.
- A manual browser pass looked right — but it ran in **Blink at a fixed 1512px
  viewport with a mouse**, and the mobile layout was faked with an injected
  stylesheet.

Neither could see the actual defect, which was the *state of the page in a
real browser at a real phone width*. That gap is what this skill closes.

## The rule that was broken

**UI whose hidden state is managed by JavaScript must fail closed.**

The drawer's "closed" state depended on two things going right: Alpine
booting, *and* the compiled CSS carrying `[x-cloak]`. When both failed the
drawer rendered open — and its close button is also Alpine, so there was no
way out. The old vanilla-JS version had a static `hidden` class and needed
zero things to go right.

Concretely, in this repo:

- `[x-cloak]{display:none !important}` is **inlined in `base.html`'s `<head>`**,
  not only in `app.css`. It must not depend on a stylesheet being built,
  purged correctly, or fetched.
- Any new overlay, drawer, modal or dropdown gets the same treatment, plus a
  spec in `tests/e2e/mobile-drawer.spec.js` style asserting it stays closed
  when its dependencies fail to load.

## Production sends a CSP; dev does not

The single biggest blind spot, and the one that actually caused the outage.
`prod.py` sets `script-src 'self' 'unsafe-inline'` with **no `'unsafe-eval'`**.
Alpine 3 compiles every expression with `new Function()`, so on the live site
all of it throws while `x-cloak` still gets stripped.

Apply `applyProdCSP(page)` from `helpers.js` in any spec covering interactive
behaviour, and keep `csp.spec.js`'s "no script is blocked by the policy" check
green. A local pass proves nothing about production unless the header is there.

`scoreboard.html` is still Alpine and does not work in production. That is a
known, unresolved gap -- do not report it as working.

## What pytest can and cannot see

| Question | pytest | Playwright |
| --- | --- | --- |
| Does the template render the right markup? | yes | — |
| Did the view pass the right context? | yes | — |
| Is the element actually on screen? | **no** | yes |
| Did Alpine initialise? | **no** | yes |
| What happens at 390px in Safari? | **no** | yes |
| Does a class survive the Tailwind purge? | **no** | yes |
| Does it work with Reduce Motion on? | **no** | yes |

If a claim belongs in the right-hand column, a pytest assertion on the markup
string is not evidence for it. Say so plainly rather than implying coverage.

## Running the suite

```bash
npm run test:e2e                       # everything
npm run test:e2e -- --project=iphone-safari
npm run test:e2e -- mobile-drawer      # one file
npm run test:e2e:ui                    # interactive, for debugging
npx playwright show-report             # last run's HTML report
```

`scripts/e2e_server.sh` owns the server: it rebuilds the CSS (so the suite
never runs against a stale stylesheet), migrates a **throwaway SQLite file**
via `TTSTATS_SQLITE_NAME`, clears `DATABASE_URL` so it can never touch the
Supabase clone of prod, and seeds a fixture with `manage.py seed_e2e`.

### Projects, and why each exists

- **`iphone-safari`** — real WebKit, iPhone 13 viewport, touch. The only
  configuration that resembles how this app is used.
- **`iphone-no-js`** — scripting disabled. The limit case of "the framework
  did not boot", and trivially reproducible unlike a specific iOS quirk.
- **`iphone-reduced`** — Reduce Motion, which a great many iPhone users have
  switched on. Not an edge case.
- **`desktop-chrome`** — the wide layout, so the sidebar path stays covered.

## Traps this suite has already hit

- **Login is rate limited to 5 POSTs / IP / 15 min.** Logging in per test makes
  the suite fail from the sixth test with a timeout that looks like a UI bug.
  A `setup` project logs in once and saves `storageState`; specs never log in.
- **`toBeVisible()` resolves at the *start* of a transition.** Measuring
  geometry there reports the panel at `-256` mid-slide and looks like an app
  bug. Use `waitForDrawerSettled()` (`helpers.js`), which polls the box.
- **`use.reducedMotion` did not reach `matchMedia` in WebKit.** Set it with
  `page.emulateMedia()` in `beforeEach`, and keep the spec that asserts the
  preference actually landed — otherwise the suite quietly tests nothing.
- **Nav link text exists three times** (sidebar, drawer, page body), two of
  them hidden. A bare `.first()` grabs an invisible node and times out. Scope
  to `main` or to the specific component.
- **The live match carries state between specs.** Read the current score and
  assert a delta; never assume 0-0.

## Adding a spec

Put it in `tests/e2e/`, named for the project that should run it:
`*.nojs.spec.js`, `*.reduced.spec.js`, `*.desktop.spec.js`, or plain
`*.spec.js` for `iphone-safari`.

Prove the spec fails against the broken code before trusting it. Comment out
the fix, watch it go red, restore. A regression test that has never failed is
a guess.

## Before you say a frontend change is verified

1. `cd ttstats && ../.venv/bin/python -m pytest -q` — markup and view logic.
2. `npm run test:e2e` — the browser reality.
3. Anything you could **not** exercise (haptics, a real device, an OS setting
   you cannot emulate) gets stated as unverified. Do not let "looks right in a
   screenshot" stand in for a test.
4. If the change is mobile-only in effect and the e2e suite does not cover it,
   it is not verified — get it covered or say so before merging.

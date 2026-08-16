# CLAUDE.md - TTStats Project Documentation

## Quick Reference

- **Project:** TTStats (Table Tennis Stats Tracker)
- **Stack:** Django 6.0, PostgreSQL 16, Redis 7, Tailwind CSS (v3, compiled), Docker
- **Python Version:** 3.12
- **Main App:** `ttstats/pingpong/`
- **Test Framework:** pytest + pytest-django + factory-boy
- **Virtual environment folder to use for python:** `.venv/`

## Common Commands

```bash
# Frontend assets (required for a styled, working page)
npm install                                        # Once, after cloning
npm run build                                      # Vendor JS + compile CSS
npm run watch:css                                  # Rebuild CSS on template change

# Development
docker compose -f compose.dev.yml up --build       # Start dev environment (includes the assets watcher)
docker compose -f compose.dev.yml exec web python manage.py migrate  # Run migrations
docker compose -f compose.dev.yml exec web python manage.py createsuperuser

# Testing (always use pytest, never Django's manage.py test)
# Can also run tests inside Docker: docker compose -f compose.dev.yml exec web python -m pytest
cd ttstats && python -m pytest --tb=short -q          # Run all tests
cd ttstats && python -m pytest --co -q                # List all tests
cd ttstats && python -m pytest ttstats/pingpong/tests/test_models.py  # Single file
cd ttstats && python -m pytest -k "TestMatch"         # Run by name pattern
cd ttstats && python -m pytest --tb=long -x           # Stop on first failure, full traceback

# Coverage (or use the helper: cd ttstats && ../scripts/run_tests.sh)
cd ttstats && coverage run -m pytest && coverage report
cd ttstats && coverage html

# Management Commands
cd ttstats && python manage.py recalculate_elo                  # Recalculate all Elo ratings
cd ttstats && python manage.py recalculate_elo --dry-run        # Preview Elo changes
cd ttstats && python manage.py cache_control --stats            # View Redis cache statistics
cd ttstats && python manage.py cache_control --clear            # Clear all caches
cd ttstats && python manage.py cache_control --test             # Test cache connectivity
cd ttstats && python manage.py warm_cache                       # Pre-populate common caches

# Production
docker compose -f compose.prod.yml up --build -d
```

---

## Testing Strategy & Rules

**This section is mandatory. Follow these rules for ALL test-related work.**

### Stack & Configuration

- **Framework:** pytest (configured in `pytest.ini` at project root)
- **Factories:** factory-boy (`conftest.py` has `UserFactory`, `PlayerFactory`, `LocationFactory`, `MatchFactory`, `GameFactory`, `ScheduledMatchFactory`, `ChampionshipFactory`)
- **Settings:** `DJANGO_SETTINGS_MODULE = ttstats.settings.test`, `pythonpath = ttstats`
- **NEVER** use Django's `TestCase` or `manage.py test`. Always use pytest classes and functions.

When adding new source code, **always create or update the corresponding test file** (convention: `test_<module>.py`).

### Test Style & Conventions

```python
import pytest
from .conftest import UserFactory, PlayerFactory, MatchFactory  # Import factories

@pytest.mark.django_db
class TestSomething:
    def test_descriptive_name(self):
        # ARRANGE: use factories, not raw ORM calls
        user = UserFactory()
        player = PlayerFactory(with_user=True)

        # ACT
        result = player.user_can_edit(user)

        # ASSERT: use plain assert, not self.assertEqual
        assert result is False
```

**Rules:**
- Always use `@pytest.mark.django_db` on test classes (or individual functions).
- Use factories from `conftest.py` to create test data. Never use raw `Model.objects.create()` except when testing the ORM itself.
- Use plain `assert` statements, not `self.assertEqual` / `self.assertTrue`.
- Group tests in classes named `Test<Subject>` (e.g., `TestMatch`, `TestGameForm`).
- Name tests `test_<what_it_verifies>` with descriptive names.
- Use helper functions (e.g., `_verified_user_with_player()`) for repeated setup, not `setUp` methods.
- For view tests that render templates, every logged-in user **must** have a linked Player profile because `base.html` unconditionally renders `{% url 'pingpong:player_detail' user.player.pk %}`. Use `_staff_with_player()` for staff test users.

### Factory Reference (`conftest.py`)

```python
UserFactory(username="...", is_staff=True, ...)  # Creates User via create_user(), password="testpass123"
PlayerFactory(name="...", with_user=True)        # with_user=True creates and links a User
LocationFactory(name="...")
MatchFactory(player1=p1, player2=p2, best_of=5)  # Singles: one player per side
MatchFactory(team1_players=[p1,p2], team2_players=[p3,p4], is_double=True)  # Doubles match
MatchFactory(confirmed=True)                     # Auto-confirms match after creation
GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
ScheduledMatchFactory(player1=p1, player2=p2, scheduled_date=date, scheduled_time=time)
ChampionshipFactory(name="...", with_entries=[[p1], [p2]], created_by=player)  # Round-robin championship
```

The `team1_players`/`team2_players` kwarg names are historical; they set side 1
and side 2 participants. `ChampionshipFactory(with_participants=...)` is an alias
for `with_entries` kept so older call sites keep reading naturally -- both take
lists of player lists.

Key fixtures:
- `complete_match` - Finished singles match (3-0 side 1 wins, best of 5)
- Helper functions: `confirm_match(match)`, `confirm_match_silent(match)`,
  `create_match(side1_players, side2_players, **kwargs)` (for raw-ORM style tests)

### Known Gotchas

1. **Participant-based architecture.** A match's players are `MatchParticipant(match, player, side)` rows; `side` is `Side.ONE`/`Side.TWO`. There is no Team model. Read them via `match.side1_players` / `match.side2_players` / `match.players_on(side)` / `match.all_players`, and write them via `services.set_match_sides(match, side1_players, side2_players)` -- never by touching participant rows directly. `Match.winner_side` and `Game.winner_side` are ints, not FKs; compare with `Side.ONE`. `ScheduledMatch` mirrors all of this via `ScheduledMatchParticipant`.
2. **MatchManager filters by current user.** In view tests, `Match.objects.get(pk=...)` only returns matches the logged-in user can see. A regular user can't see matches they're not in — `get_object_or_404(Match, pk=pk)` returns 404, not 403.
3. **Signals fire on User creation.** Every `UserFactory()` call creates a `UserProfile` with a verification token via signal. You don't need to create profiles manually.
4. **Game.save() triggers Match.save().** Creating enough games automatically sets the match winner. Tests that check "no winner yet" must not create too many games.
5. **Match confirmations use a junction table.** Singles require 2 confirmations (both players), doubles require 4 (all players) -- but only *verified* players count. Use `confirmed=True` in MatchFactory or the `confirm_match()` helper.
6. **Elo updates on confirmation.** Elo ratings only change when match is fully confirmed. Use `confirm_match()` or `confirm_match_silent()` in tests to trigger Elo calculation.
7. **Manager tests need thread-local manipulation.** Import `_thread_locals` from `ttstats.middleware` and set/clear `_thread_locals.user` directly. Use an `autouse` fixture to clean up.
8. **base.html requires user.player.pk.** Any view test where the user has no Player profile will crash during template rendering with `NoReverseMatch`. Always create a player for the test user.
9. **Email backend in tests.** Dev settings use `console.EmailBackend`. pytest-django's `mailoutbox` fixture or `django.core.mail.outbox` works for asserting sent emails.
10. **LocMemCache persists between tests.** Django's LocMemCache persists across pytest tests in the same process. An autouse `_clear_cache` fixture in `conftest.py` calls `cache.clear()` before and after each test.

### TDD Workflow for New Features

1. **Write failing tests first.** Run `python -m pytest path/to/test_file.py` and confirm they fail.
2. **Implement the minimum code to pass.** Run the tests again and iterate until green.
3. **Refactor if needed.** Clean up while tests stay green.
4. **Add edge-case tests.** Cover error paths, boundary conditions, permission checks.
5. **Run the full suite.** `python -m pytest --tb=short -q` before considering the work done.

### Unit Tests: Test Each Piece Individually

- **Models:** Test methods, properties, and constraints in isolation. Don't test via views.
- **Forms:** Instantiate the form with `data={}` directly. Don't go through HTTP requests.
- **Views:** Use the Django test `Client`. Assert on status codes, context data, redirects, and messages.
- **Managers:** Manipulate thread-local user directly. Don't use the test client.
- **Signals:** Create/save model instances and assert side effects (emails sent, profiles created).
- **Emails:** Call the email function directly, check `mail.outbox`.
- **Middleware:** Use `RequestFactory` to create mock requests. Don't use the full test client.
- **Context processors:** Use `RequestFactory` with an explicit user. Don't render templates.

### Integration Tests: Protect Core Happy Paths

Maintain integration tests that exercise complete user flows end-to-end through multiple view calls in sequence. **When adding a new feature**, also add an integration test covering its primary happy path.

**Required integration test flows** (in `test_views.py` or `test_integration.py`):

1. **Registration -> Verification -> Login:** POST signup -> GET verify-email with token -> POST login -> assert dashboard loads.
2. **Match lifecycle (singles with Elo):** Create match -> add games (triggers winner) -> assert emails sent -> confirm as both players -> assert Elo updated -> verify leaderboard.
3. **Doubles match lifecycle:** Create match (4 players) -> add games -> confirm as all 4 players -> assert Elo updated for all.
4. **Scheduled match conversion:** Schedule match -> assert emails -> convert to match -> add games -> confirm -> verify calendar status.
5. **Head-to-head with data:** Create two players, play multiple confirmed matches -> GET head-to-head -> assert stats correct.

---

## Business Logic Gotchas

These are non-obvious behaviors that aren't clear from reading individual source files.

### Row-Level Security
- `CurrentUserMiddleware` stores user in thread-local (`_thread_locals.user`)
- Managers filter querysets: no user = unfiltered, anonymous = empty, staff = all, regular = own matches only
- **Bypass:** Use `Model.all_objects` (e.g., `Match.all_objects`) to skip row-level filtering (needed in championship views)

### Match Confirmation & Elo
- When winner is set for the first time: if any player is unverified, auto-confirm all; if all verified, send confirmation emails
- `match_confirmed` property: True when all verified players confirmed. Empty verified set = True (unverified players don't need confirmation)
- Elo ratings only update when match is fully confirmed
- **`Match.recompute()` is the single source of truth** for `winner_side`, the score caches and `is_confirmed`. It writes via a queryset `.update()` so it cannot re-enter the signal pipeline. Never set those fields by hand; call `recompute()`.
- The pure decision functions live in `match_state.py` (no Django imports): `winner_side()`, `side_confirmed()`, `confirmation_complete()`, `should_auto_confirm()`. Test them without a DB.
- Views should use `Match.objects.filter(is_confirmed=True)` (DB-level), not Python-level filtering

### Championship System
- Championship matches may have winners but `is_confirmed=False` — always filter by `winner_side__isnull=False` for championship data, not `is_confirmed=True`
- Use `Match.all_objects` and `ScheduledMatch.all_objects` in championship views to bypass row-level security
- `ScheduledMatchConvertView.form_valid()` auto-sets `match.championship` FK when converting championship scheduled matches
- `check_completion()` auto-transitions to `completed` when all scheduled matches are converted and confirmed
- Round-robin pairing is a pure function in `championship_scheduling.py` (`round_robin_rounds`, `round_robin_double_rounds`) -- circle method, home + away (andata e ritorno). Test it without a DB.
- Entrants are `ChampionshipEntry` + `ChampionshipEntryMember`, not teams. The denormalized `championship` FK on the member row lets the DB enforce one entry per player per championship. Register via `championship.register_entry(players)`.
- **`generate_schedule()` uses `bulk_create`, which bypasses `post_save`** — it therefore builds `ScheduledMatchParticipant` rows explicitly. Any new bulk path must do the same or the schedule comes out with no participants.

### Frontend Build
- Tailwind is **compiled**, not loaded from a CDN. Source `pingpong/assets/app.css` (kept outside `static/` so collectstatic never copies the raw `@tailwind` source), output `static/pingpong/css/app.css` (gitignored). Config in `tailwind.config.js` at the repo root.
- **Pinned to Tailwind v3.** The palette is a v3 `theme.extend.colors` object lifted from the old inline `base.html` config; v4's CSS-first config would be a rewrite.
- **A class Tailwind cannot see is silently dropped.** The `content` globs cover all templates *and* `pingpong/*.py`, because `forms.py` still builds widget class strings in Python. If you move class names into a new Python module, add it to the globs.
- Tests do not need the CSS to exist; `{% static %}` resolves without it. The browser does -- run `npm run build:css` after cloning.
- Docker: a `node:20-alpine AS assets` stage builds the CSS, and the `COPY --from=assets` in the final stage must stay **after** `COPY ttstats/ .` or it gets overwritten. `compose.dev.yml` bind-mounts `./ttstats` over `/app`, which shadows the image's CSS -- hence the separate `assets` watcher service.
- **Static storage is `STORAGES`, not `STATICFILES_STORAGE`** (Django 5.1 removed the latter). Only `prod.py` uses WhiteNoise's hashing manifest storage; dev and tests use plain storage, because manifest storage refuses to resolve any file absent from a collectstatic-built manifest.
- **Vendored files must ship their `.map` siblings.** Manifest storage resolves every `sourceMappingURL` and hard-fails `collectstatic` if the target is missing -- and `entrypoint.sh` runs `collectstatic` under `set -e`, so a missing map breaks the deploy, not just the page.
- CI builds the assets when either Python or frontend files change. Before this, a template-only commit got zero CI.

### Vendored JavaScript
- htmx, Alpine, Chart.js and Tom Select are **served from `self`**, not a CDN. `npm run vendor` copies their browser builds out of `node_modules` into `static/pingpong/js/vendor/` and `static/pingpong/css/vendor/` (both gitignored). `npm run build` = vendor + CSS.
- Versions are pinned in `package-lock.json`, not in a URL.
- **htmx loads before Alpine** in `base.html`, so Alpine's deferred init sees any markup htmx already swapped in.
- `<body>` carries `hx-headers` with the CSRF token, so every htmx request is authenticated without per-element wiring.
- `prod.py`'s CSP now allows no external hosts at all. `'unsafe-inline'` is still required for scripts and styles because templates carry inline `<script>` blocks; B.5 is what removes them.

### Forms
- **Never hand-write `<option>` loops.** Render the widget (`{{ form.field }}`) or include `pingpong/_field.html`. A hand-written loop comparing `form.f.value` to a literal is wrong on a bound form: the POST value is a *string*, so `"7" == 7` is false and the user's choice disappears when validation fails.
- `_field.html` renders label, widget, help text and **every** error (templates used to print `.errors.0` and drop the rest). Optional context: `label`, `help`, `badge`, `wrapper_class`.
- `StyledFormMixin` (forms.py) puts `.field-input` on every widget and sets `error_css_class`. Add it to any new form; do not paste Tailwind strings into Python.
- **Adding classes in a view:** use `append_widget_class(field, css)`. `widget.attrs.update({"class": ...})` replaces the attribute and silently drops `field-input`.
- Choice lists belong in forms.py (`BEST_OF_CHOICES`), not in template markup.

### Template / Frontend
- `base.html` unconditionally renders `{% url 'pingpong:player_detail' user.player.pk %}` — every authenticated user **must** have a linked Player profile
- Use `json_script` template tag for passing data to JavaScript, NOT `escapejs` (causes double-serialization)
- Chart.js colors: use explicit `rgb()` values (e.g., `rgb(59, 130, 246)`), not CSS custom properties (render as black)

### Passkey Authentication
- Optional WebAuthn/FIDO2 via django-otp + django-otp-webauthn
- **Use localhost, not 127.0.0.1** for dev — WebAuthn rejects IP addresses
- HTTPS required in production
- Required button IDs: `passkey-register-button`, `passkey-register-status-message`, `passkey-registration-placeholder`, `passkey-verification-button`, `passkey-verification-status-message`, `passkey-verification-placeholder`
- Template must include `<template id="...-available-template">` and `<template id="...-unavailable-template">` elements
- `PasskeyManagementView` handles missing `django_otp_webauthn` with try/except import
- Admin inline: staff can view/delete passkeys but cannot add them (security requirement)

### Redis Cache
- Falls back to LocMemCache when `REDIS_URL` is not set
- Leaderboard uses generation counter pattern: `cache.incr('leaderboard_generation')` versions keys instead of deleting all filter variants
- `CacheDebugMiddleware` (dev only) adds `X-Request-Time`, `X-Cache-Hits`, `X-Cache-Misses` headers

## Environment & Deployment

- **Dev:** `DEBUG=True`, SQLite, Console email, `DJANGO_SETTINGS_MODULE=ttstats.settings.dev`
- **Prod:** `DEBUG=False`, PostgreSQL, Mailgun, HTTPS, WhiteNoise, `DJANGO_SETTINGS_MODULE=ttstats.settings.prod`
- **Docker services:** web (Django), db (PostgreSQL), redis (Redis 7 Alpine)
- **CI/CD** (`.github/workflows/main.yml`): On push/PR runs tests with coverage; on master push deploys via SSH to VPS

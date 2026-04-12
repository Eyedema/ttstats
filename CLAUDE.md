# CLAUDE.md - TTStats Project Documentation

## Quick Reference

- **Project:** TTStats (Table Tennis Stats Tracker)
- **Stack:** Django 6.0, PostgreSQL 16, Redis 7, Tailwind CSS, Docker
- **Python Version:** 3.12
- **Main App:** `ttstats/pingpong/`
- **Test Framework:** pytest + pytest-django + factory-boy
- **Virtual environment folder to use for python:** `.venv/`

## Common Commands

```bash
# Development
docker compose -f compose.dev.yml up --build       # Start dev environment
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
- **Factories:** factory-boy (`conftest.py` has `UserFactory`, `PlayerFactory`, `LocationFactory`, `TeamFactory`, `MatchFactory`, `GameFactory`, `ScheduledMatchFactory`, `ChampionshipFactory`)
- **Settings:** `DJANGO_SETTINGS_MODULE = ttstats.settings.dev`, `pythonpath = ttstats`
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
TeamFactory(players=[p1])                        # Creates team with 1+ players
MatchFactory(player1=p1, player2=p2, best_of=5)  # Backward-compatible, creates 1-player teams
MatchFactory(team1_players=[p1,p2], team2_players=[p3,p4], is_double=True)  # Doubles match
MatchFactory(confirmed=True)                     # Auto-confirms match after creation
GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
ScheduledMatchFactory(player1=p1, player2=p2, scheduled_date=date, scheduled_time=time)
ChampionshipFactory(name="...", with_participants=[t1, t2], created_by=player)  # Round-robin championship
```

Key fixtures:
- `complete_match` - Finished singles match (3-0 player1 wins, best of 5)
- Helper functions: `confirm_match(match)`, `confirm_match_silent(match)`, `confirm_team(team, match)`

### Known Gotchas

1. **Team-based architecture.** Matches use Team model (not direct player references). Singles = 1-player teams, doubles = 2-player teams. Use `player1`/`player2` kwargs in MatchFactory for backward compatibility, or `team1_players`/`team2_players` for explicit control.
2. **MatchManager filters by current user.** In view tests, `Match.objects.get(pk=...)` only returns matches the logged-in user can see. A regular user can't see matches they're not in — `get_object_or_404(Match, pk=pk)` returns 404, not 403.
3. **Signals fire on User creation.** Every `UserFactory()` call creates a `UserProfile` with a verification token via signal. You don't need to create profiles manually.
4. **Game.save() triggers Match.save().** Creating enough games automatically sets the match winner. Tests that check "no winner yet" must not create too many games.
5. **Match confirmations use junction table.** Singles require 2 confirmations (both players), doubles require 4 (all players). Use `confirm=True` in MatchFactory or `confirm_match()` helper.
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
- `is_confirmed` denormalized field must be updated in ALL signal paths (auto-confirm AND email paths). When all players are unverified, `should_auto_confirm()` returns False because `match_confirmed` is already True.
- Use `Match.objects.filter(pk=instance.pk).update(is_confirmed=...)` in signals to avoid re-triggering pre/post_save signals
- Views should use `Match.objects.filter(is_confirmed=True)` (DB-level), not Python-level filtering

### Championship System
- Championship matches may have winners but `is_confirmed=False` — always filter by `winner__isnull=False` for championship data, not `is_confirmed=True`
- Use `Match.all_objects` and `ScheduledMatch.all_objects` in championship views to bypass row-level security
- `ScheduledMatchConvertView.form_valid()` auto-sets `match.championship` FK when converting championship scheduled matches
- `check_completion()` auto-transitions to `completed` when all scheduled matches are converted and confirmed
- Round-robin schedule uses circle method: generates home + away rounds (andata e ritorno)

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

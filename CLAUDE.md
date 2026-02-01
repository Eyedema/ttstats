# CLAUDE.md - TTStats Project Documentation

## Quick Reference

- **Project:** TTStats (Table Tennis Stats Tracker)
- **Stack:** Django 6.0, PostgreSQL 16, Tailwind CSS, Docker
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
cd ttstats && python -m pytest --tb=short -q          # Run all tests
cd ttstats && python -m pytest --co -q                # List all tests
cd ttstats && python -m pytest ttstats/pingpong/tests/test_models.py  # Single file
cd ttstats && python -m pytest -k "TestMatch"         # Run by name pattern
cd ttstats && python -m pytest --tb=long -x           # Stop on first failure, full traceback

# Coverage
cd ttstats && coverage run -m pytest && coverage report         # Run with coverage
cd ttstats && coverage html                                     # Generate HTML report


# Production
docker compose -f compose.prod.yml up --build -d
```

## Project Structure

```
/Users/ubaldopuocci/ttstats/
 ├── pytest.ini                        # Pytest configuration (DO NOT use manage.py test) 
├── requirements.txt                  # Python dependencies
├── ttstats/                          # Django project root
│   ├── manage.py                     # Django CLI
│   ├── pingpong/                     # Main application
│   │   ├── models.py                 # Database models (6 models)
│   │   ├── views.py                  # View classes (18 views, ~1060 lines)
│   │   ├── forms.py                  # Form definitions (5 forms)
│   │   ├── urls.py                   # URL routing
│   │   ├── signals.py                # Django signals
│   │   ├── managers.py               # Custom QuerySet managers
│   │   ├── emails.py                 # Email utilities
│   │   ├── admin.py                  # Django admin config
│   │   ├── context_processors.py     # Template context
│   │   ├── migrations/               # Database migrations (8 total)
│   │   ├── templates/pingpong/       # Django templates
│   │   ├── templates/registration/   # Auth templates
│   │   ├── static/pingpong/icons/    # SVG icons (800+)

│   │   └── tests/                    # Test suite (pytest + factory-boy)
│   │       ├── conftest.py           # Factories + shared fixtures
│   │       ├── test_models.py        # Model tests
│   │       ├── test_forms.py         # Form validation tests
│   │       ├── test_views.py         # View tests (status codes, auth, context, redirects)
│   │       ├── test_signals.py       # Signal handler tests
│   │       ├── test_managers.py      # Custom manager tests (row-level security)
│   │       ├── test_emails.py        # Email utility tests
│   │       ├── test_middleware.py    # Middleware tests
│   │       └── test_context_processors.py  # Context processor tests

│   └── ttstats/                      # Django configuration
│       ├── settings/
│       │   ├── base.py               # Base settings
│       │   ├── dev.py                # Development settings
│       │   └── prod.py               # Production settings
│       ├── urls.py                   # Root URL config
│       ├── middleware.py             # CurrentUserMiddleware
│       └── wsgi.py / asgi.py         # Application servers
├── docker/django/                    # Docker configuration
│   ├── Dockerfile                    # Multi-stage build
│   └── entrypoint.sh                 # Container entrypoint
├── compose.dev.yml                   # Development compose
├── compose.prod.yml                  # Production compose
├── .env.dev                          # Dev environment vars
├── .env.prod.example                 # Prod env template
├── .github/workflows/main.yml        # CI/CD pipeline
 └── .coveragerc                       # Coverage config 
```

---


## Testing Strategy & Rules

**This section is mandatory. Follow these rules for ALL test-related work.**

### Stack & Configuration

- **Framework:** pytest (configured in `pytest.ini` at project root)
- **Factories:** factory-boy (`conftest.py` has `UserFactory`, `PlayerFactory`, `LocationFactory`, `MatchFactory`, `GameFactory`, `ScheduledMatchFactory`)
- **Settings:** `DJANGO_SETTINGS_MODULE = ttstats.settings.dev`, `pythonpath = ttstats`
- **NEVER** use Django's `TestCase` or `manage.py test`. Always use pytest classes and functions.

### Test File Organization

Each source module has a corresponding test file:

| Source file | Test file | What to test |
|-------------|-----------|-------------|
| `models.py` | `test_models.py` | Field defaults, `__str__`, properties, methods, ordering, constraints, cascades |
| `forms.py` | `test_forms.py` | Valid/invalid data, custom clean methods, field-level validation |
| `views.py` | `test_views.py` | Status codes, auth redirects, template used, context data, form handling, messages |
| `signals.py` | `test_signals.py` | Signal side effects, conditional logic, no double-triggers |
| `managers.py` | `test_managers.py` | Row-level security filtering per user role |
| `emails.py` | `test_emails.py` | Email content, recipients, early returns, settings fallbacks |
| `middleware.py` | `test_middleware.py` | Thread-local set/cleanup, exception safety |
| `context_processors.py` | `test_context_processors.py` | Context dict values per auth state |

When adding new source code, **always create or update the corresponding test file**.

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
PlayerFactory(name="...", with_user=True)         # with_user=True creates and links a User
LocationFactory(name="...")
MatchFactory(player1=p1, player2=p2, best_of=5)   # Players auto-created with users by default
GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
ScheduledMatchFactory(player1=p1, player2=p2, scheduled_date=date, scheduled_time=time)
```

Key fixture: `complete_match` gives you a finished match (3-0 player1 wins, best of 5).

### Known Gotchas

1. **MatchManager filters by current user.** In view tests, `Match.objects.get(pk=...)` only returns matches the logged-in user can see. A regular user can't see matches they're not in — `get_object_or_404(Match, pk=pk)` returns 404, not 403.
2. **Signals fire on User creation.** Every `UserFactory()` call creates a `UserProfile` with a verification token via signal. You don't need to create profiles manually.
3. **Game.save() triggers Match.save().** Creating enough games automatically sets the match winner. Tests that check "no winner yet" must not create too many games.
4. **Manager tests need thread-local manipulation.** Import `_thread_locals` from `ttstats.middleware` and set/clear `_thread_locals.user` directly. Use an `autouse` fixture to clean up.
5. **base.html requires user.player.pk.** Any view test where the user has no Player profile will crash during template rendering with `NoReverseMatch`. Always create a player for the test user.
6. **Email backend in tests.** Dev settings use `console.EmailBackend`. pytest-django's `mailoutbox` fixture or `django.core.mail.outbox` works for asserting sent emails.

### TDD Workflow for New Features

Follow this order when implementing new features:

1. **Write failing tests first.** Create or update the test file for the module you're changing. Write tests that describe the expected behavior. Run `python -m pytest path/to/test_file.py` and confirm they fail.
2. **Implement the minimum code to pass.** Write the model/form/view/signal code. Run the tests again and iterate until green.
3. **Refactor if needed.** Clean up while tests stay green.
4. **Add edge-case tests.** Cover error paths, boundary conditions, permission checks.
5. **Run the full suite.** `python -m pytest --tb=short -q` before considering the work done.

### Unit Tests: Test Each Piece Individually

- **Models:** Test methods, properties, and constraints in isolation. Don't test via views.
- **Forms:** Instantiate the form with `data={}` directly. Don't go through HTTP requests.
- **Views:** Use the Django test `Client` to exercise HTTP request/response. Assert on status codes, context data, redirects, and messages.
- **Managers:** Manipulate thread-local user directly. Don't use the test client.
- **Signals:** Create/save model instances and assert side effects (emails sent, profiles created).
- **Emails:** Call the email function directly, check `mail.outbox`.
- **Middleware:** Use `RequestFactory` to create mock requests. Don't use the full test client.
- **Context processors:** Use `RequestFactory` with an explicit user. Don't render templates.

### Integration Tests: Protect Core Happy Paths

Beyond unit tests, maintain integration tests that exercise complete user flows end-to-end through multiple view calls in sequence. These ensure that the pieces work together correctly and that core functionality never silently breaks.

**Required integration test flows** (add these to `test_views.py` or a dedicated `test_integration.py`):

1. **Registration -> Verification -> Login flow:**
   POST signup -> GET verify-email with token -> POST login -> assert dashboard loads with correct user context.

2. **Match lifecycle:**
   POST create match -> POST add game 1 -> POST add game 2 -> POST add game 3 (triggers winner) -> assert match complete, winner set, confirmation emails sent -> POST confirm as player1 -> POST confirm as player2 -> assert match_confirmed is True -> GET leaderboard -> assert stats reflect the match.

3. **Scheduled match flow:**
   POST schedule match -> assert emails sent -> GET calendar with correct month -> assert match appears on correct day.

4. **Head-to-head with data:**
   Create two players, play multiple confirmed matches between them -> GET head-to-head with both player IDs -> assert all stats (game wins, margins, streaks) are calculated correctly.

These integration tests simulate real user sessions. They catch regressions where individual units pass but the wiring between them breaks (wrong redirect URL, missing context variable, signal not firing in the right order, etc.).

**When adding a new feature**, also add an integration test covering its primary happy path alongside the unit tests.


---

## Database Models

### Location (`pingpong/models.py`)
```python
# Fields: name, address, notes, created_at
# Ordering: by name
```

### Player (`pingpong/models.py`)
```python
# Fields: user (optional OneToOne), name, nickname, playing_style, notes, created_at
# playing_style choices: normal, hard_rubber, unknown
# Methods: user_can_edit(user)
# Manager: PlayerManager (all visible, editable_by() for filtering)
```

### Match (`pingpong/models.py`)
```python
# Fields: player1, player2, date_played, location, match_type, best_of, winner,
#         player1_confirmed, player2_confirmed, notes, created_at, updated_at
# match_type choices: casual, practice, tournament
# best_of choices: 3, 5, 7
# Properties: player1_score, player2_score (game win counts), match_confirmed
# Methods: user_can_edit(user), user_can_view(user), should_auto_confirm(),
#          get_unverified_players()
# save() auto-determines winner from games when match already exists (has pk)
# Manager: MatchManager (row-level security based on user)
```

### Game (`pingpong/models.py`)
```python
# Fields: match, game_number, player1_score, player2_score, winner, duration_minutes
# Constraint: unique (match, game_number)
# save() auto-determines winner from scores, then calls match.save() to update match winner
# Manager: GameManager (filters by match visibility)
```

### UserProfile (`pingpong/models.py`)
```python
# Fields: user (OneToOne), email_verified, email_verification_token,
#         email_verification_sent_at, created_at
# Auto-created via signal when User is created (with verification token)
# Methods: create_verification_token(), verify_email(token)
```

### ScheduledMatch (`pingpong/models.py`)
```python
# Fields: player1, player2, scheduled_date, scheduled_time, location, notes,
#         created_at, created_by, notification_sent
# Property: scheduled_datetime (combines date + time)
# Methods: user_can_view(user), user_can_edit(user) (delegates to user_can_view)
# Manager: ScheduledMatchManager (row-level security based on user)
# Ordering: by scheduled_date, scheduled_time
```

## URL Routes

### Authentication
| Method | URL | View | Description |
|--------|-----|------|-------------|
| POST | `/accounts/login/` | CustomLoginView | User login (blocks unverified) |
| GET/POST | `/pingpong/signup/` | PlayerRegistrationView | User registration |
| GET | `/pingpong/verify-email/<token>/` | EmailVerifyView | Email verification |
| POST | `/pingpong/resend-verification/` | EmailResendVerificationView | Resend token |
| POST | `/accounts/logout/` | Django built-in | Logout |

### Core Pages (all LoginRequired)
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET | `/pingpong/` | DashboardView | Main dashboard |
| GET | `/pingpong/leaderboard/` | LeaderboardView | Player rankings |
| GET | `/pingpong/head-to-head/` | HeadToHeadStatsView | Player comparison |
| GET | `/pingpong/calendar/` | CalendarView | Calendar with scheduled/past matches |

### Players (all LoginRequired)
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET | `/pingpong/players/` | PlayerListView | List all players (paginated, 10/page) |
| GET/POST | `/pingpong/players/add/` | PlayerCreateView | Create player form |
| GET | `/pingpong/players/<id>/` | PlayerDetailView | Player details + stats |
| GET/POST | `/pingpong/players/<id>/edit/` | PlayerUpdateView | Edit player |

### Matches (all LoginRequired)
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET | `/pingpong/matches/` | MatchListView | List matches |
| GET/POST | `/pingpong/matches/add/` | MatchCreateView | Create match |
| GET | `/pingpong/matches/<id>/` | MatchDetailView | Match details |
| GET/POST | `/pingpong/matches/<id>/edit/` | MatchUpdateView | Edit match |
| POST | `/pingpong/match/<id>/confirm/` | match_confirm | Confirm participation |
| POST | `/pingpong/matches/<match_id>/add-game/` | GameCreateView | Add game to match |
| GET/POST | `/pingpong/matches/schedule/` | ScheduledMatchCreateView | Schedule future match |

## Forms Reference (`pingpong/forms.py`)

| Form | Model | Fields | Validation |
|------|-------|--------|------------|
| `MatchForm` | Match | player1, player2, date_played, location, match_type, best_of, notes | player1 != player2 |
| `MatchEditForm` | Match | location, notes | (completed matches only) |
| `GameForm` | Game | game_number, player1_score, player2_score, duration_minutes | No ties; win by 2 at deuce (>=10-10) |
| `PlayerRegistrationForm` | User | username, email, password1, password2, full_name, nickname, playing_style | Creates User + Player on save(commit=True) |
| `ScheduledMatchForm` | ScheduledMatch | player1, player2, scheduled_date, scheduled_time, location, notes | player1 != player2; date >= today |

## Signals (`pingpong/signals.py`)

| Signal | Trigger | Action |
|--------|---------|--------|
| `create_user_profile` | User post_save (created=True) | Creates UserProfile + verification token |
| `track_match_winner_change` | Match pre_save | Sets `_winner_just_set` flag if winner goes from None to set |
| `handle_match_completion` | Match post_save | If `_winner_just_set`: auto-confirm (unverified players) OR send confirmation emails (verified players) |
| `notify_passkey_registered` | WebAuthnCredential post_save (created=True) | Sends email notification when new passkey is registered |

## Business Logic

### Match Confirmation System
1. Match created with both confirmations = False
2. When winner is set for the first time (via Game.save() -> Match.save()):
   - If any player is unverified (no user or email_verified=False): auto-confirm both sides via DB update
   - If both players are verified: send confirmation emails to each
3. Match shows confirmation badges in UI

### Winner Determination
- **Game:** Automatically determined by comparing player1_score vs player2_score in Game.save()
- **Match:** Automatically determined in Match.save() when a player has >= (best_of // 2 + 1) game wins

### Row-Level Security
- `CurrentUserMiddleware` stores user in thread-local (`_thread_locals.user`)
- `MatchManager.get_queryset()`: no user = unfiltered, anonymous = empty, staff = all, regular = own matches only
- `GameManager`: mirrors match visibility
- `ScheduledMatchManager`: same pattern as MatchManager
- `PlayerManager`: all users see all players; `editable_by(user)` filters for edit permissions

### Email Verification Flow
1. User registers -> User created (signal creates UserProfile + token)
2. Registration view sends verification email with token link
3. User clicks `/pingpong/verify-email/<token>/` -> verified, auto-logged in, redirected to dashboard
4. Unverified users blocked at login (CustomLoginView.form_valid)

### Scheduled Matches
1. User creates scheduled match (player1 locked to self for non-staff)
2. Both players receive email notification
3. Match appears in calendar view
4. notification_sent flag set to True

## Templates

### Base Template Constraint
`base.html` line 169 unconditionally renders `{% url 'pingpong:player_detail' user.player.pk %}`. This means **every authenticated user must have a linked Player profile** or the page will crash with `NoReverseMatch`.  This affects both production and test code. 

### Key Templates
| Template | Purpose |
|----------|---------|
| `base.html` | Base layout with Tailwind (shadcn/ui colors), nav, alerts |
| `dashboard.html` | Stats overview |
| `match_list.html` | Match table/cards |
| `match_detail.html` | Full match view with games |
| `match_form.html` | Create/edit match |
| `player_list.html` | Player listing |
| `player_detail.html` | Player profile & stats |
| `game_form.html` | Score entry |
| `head_to_head.html` | Player comparison with charts |
| `leaderboard.html` | Rankings table |
| `calendar.html` | Calendar view with scheduled matches |
| `scheduled_match_form.html` | Schedule a future match |

## Environment Configuration

### Development (`.env.dev`)
- `DEBUG=True`, SQLite, Console email backend
- `DJANGO_SETTINGS_MODULE=ttstats.settings.dev`

### Production (`.env.prod`)
- `DEBUG=False`, PostgreSQL, Mailgun email, HTTPS, WhiteNoise
- `DJANGO_SETTINGS_MODULE=ttstats.settings.prod`

## Docker

```bash
# Development
docker compose -f compose.dev.yml up --build       # http://localhost:8000

# Production
docker compose -f compose.prod.yml up --build -d   # Gunicorn (3 workers)
```

### Entrypoint (`docker/django/entrypoint.sh`)
1. Wait for database
2. Run migrations
3. Collect static files (prod)
4. Start server

## CI/CD Pipeline (`.github/workflows/main.yml`)

### On Push/PR:
1. Setup Python 3.12
2. Install dependencies (cached)
3. Run tests with coverage
4. Post coverage report to PR comments
5. Upload HTML coverage artifact

### On Master Push (if tests pass):

### On Master Push:
1. SSH to VPS
2. Pull latest code
3. Rebuild and restart containers

## Dependencies (`requirements.txt`)

```
Django==6.0
asgiref==3.11.0
coverage==7.13.1
django-otp==1.5.4
django-otp-webauthn==0.3.0
factory-boy==3.3.1
faker==33.3.1
pytest==8.3.4
pytest-django==4.9.0
sqlparse==0.5.5
whitenoise==6.11.0
pytest-cov

```

## Key Files Quick Reference

| Need to... | File |
|------------|------|
| Add a model | `pingpong/models.py` |
| Add a view | `pingpong/views.py` |
| Add a URL | `pingpong/urls.py` |
| Add a form | `pingpong/forms.py` |
| Add a template | `pingpong/templates/pingpong/` |
| Add a signal | `pingpong/signals.py` |
| Add email logic | `pingpong/emails.py` |
| Modify security | `pingpong/managers.py` |
| Add context vars | `pingpong/context_processors.py` |
| Add admin config | `pingpong/admin.py` |
| Add/update tests | `pingpong/tests/` (see Testing Strategy section) |
| Add factories/fixtures | `pingpong/tests/conftest.py` |
| Modify settings | `ttstats/settings/` |
| Modify middleware | `ttstats/middleware.py` |

## Migration Commands

```bash
cd ttstats
python manage.py makemigrations pingpong    # Create migration
python manage.py migrate                     # Apply migrations
python manage.py showmigrations              # List migrations
```

Current migrations (8 total):
1. `0001_initial` - Initial schema
2. `0002-0004` - Location field adjustments
3. `0005` - best_of field adjustment
4. `0006` - Match confirmation fields
5. `0007` - UserProfile model
6. `0008` - ScheduledMatch model

## Passkey Authentication

TTStats supports passkey (WebAuthn/FIDO2) authentication for existing users as an optional login method.

### Features
- **Passwordless login:** Use biometrics (Face ID, Touch ID, Windows Hello) or security keys (YubiKey, etc.)
- **Optional:** Traditional password authentication remains available
- **Multiple passkeys:** Users can register multiple devices
- **Security notifications:** Email alerts when passkeys are added/removed
- **Admin visibility:** Staff can see passkey counts and manage credentials

### Stack
- **django-otp:** MFA framework (v1.5.4+)
- **django-otp-webauthn:** WebAuthn implementation (v0.3.0+)
- **py_webauthn:** FIDO2 library (installed automatically)

### User Flow
1. User logs in with password
2. Navigates to "Manage Passkeys" (`/pingpong/passkeys/`)
3. Clicks "Register Passkey" button
4. Browser prompts for biometric/security key confirmation
5. Passkey registered and linked to user account
6. User can now log in with passkey (passwordless) on future visits

### Files Added/Modified
| File | Purpose |
|------|---------|
| `requirements.txt` | Added django-otp and django-otp-webauthn |
| `settings/base.py` | Added INSTALLED_APPS, MIDDLEWARE, AUTHENTICATION_BACKENDS, WebAuthn config |
| `settings/prod.py` | Production WebAuthn origins |
| `pingpong/urls.py` | Passkey management URL + WebAuthn endpoints |
| `pingpong/views.py` | PasskeyManagementView (GET: show credentials, POST: delete) |
| `pingpong/admin.py` | CustomUserAdmin with passkey count display + PasskeyInline |
| `pingpong/signals.py` | notify_passkey_registered signal |
| `pingpong/emails.py` | send_passkey_registered_email, send_passkey_deleted_email |
| `templates/registration/login.html` | Passkey login button + WebAuthn scripts |
| `templates/pingpong/base.html` | Passkey link in navigation |
| `templates/pingpong/passkey_management.html` | NEW: Passkey management page |
| `tests/test_passkey_views.py` | NEW: Unit tests for passkey views |
| `tests/test_passkey_integration.py` | NEW: Integration tests |
| `tests/test_passkey_emails.py` | NEW: Email notification tests |
| `tests/test_passkey_admin.py` | NEW: Admin interface tests |


### URL Routes
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET/POST | `/pingpong/passkeys/` | PasskeyManagementView | Manage user's passkeys |
| POST | `/pingpong/webauthn/register/` | Library view | Register new passkey |
| POST | `/pingpong/webauthn/authenticate/` | Library view | Authenticate with passkey |

### Security Features
1. **Origin validation:** Only allowed domains can register passkeys (prevents phishing)
2. **Replay attack prevention:** Sign counter increments with each use
3. **User verification:** Passkeys require biometric/PIN confirmation
4. **Email notifications:** Users alerted when passkeys are added/removed
5. **Public key cryptography:** Private keys never leave user's device

### Testing Passkeys
```bash
# Unit tests (view logic, CRUD operations)
python -m pytest ttstats/pingpong/tests/test_passkey_views.py -v

# Integration tests (page rendering, auth flow)
python -m pytest ttstats/pingpong/tests/test_passkey_integration.py -v

# Email notification tests
python -m pytest ttstats/pingpong/tests/test_passkey_emails.py -v

# Admin interface tests
python -m pytest ttstats/pingpong/tests/test_passkey_admin.py -v

# Run all passkey tests
python -m pytest ttstats/pingpong/tests/test_passkey*.py -v
```

**Note:** Full WebAuthn ceremony testing (actual biometric prompts) requires browser automation (Selenium). The provided tests cover view logic, email notifications, and page rendering.


### Browser Compatibility
- Chrome 67+ ✓
- Firefox 60+ ✓
- Safari 13+ ✓
- Edge 18+ ✓

### Configuration
**Development:**
- `OTP_WEBAUTHN_RP_ID = "localhost"`
- `OTP_WEBAUTHN_ALLOWED_ORIGINS = ["http://localhost:8000"]`
- **IMPORTANT:** Always access dev server via `localhost:8000`, NOT `127.0.0.1:8000`
- WebAuthn rejects IP addresses (except localhost) for security reasons

**Production:**
- `OTP_WEBAUTHN_RP_ID = os.environ.get("SITE_DOMAIN")`
- `OTP_WEBAUTHN_ALLOWED_ORIGINS = [f"https://{os.environ.get('SITE_DOMAIN')}"]`
- HTTPS is required (WebAuthn doesn't work on plain HTTP in production)

### Known Gotchas
1. **Use localhost, not 127.0.0.1:** WebAuthn requires a valid domain name. IP addresses are not allowed (except localhost). Always access the dev server via `http://localhost:8000`, NOT `http://127.0.0.1:8000`
2. **HTTPS required in production:** WebAuthn only works on HTTPS (except localhost)
3. **Origin must match:** `OTP_WEBAUTHN_RP_ID` must match your domain exactly
4. **Button IDs are required:** The library's JavaScript expects specific element IDs:
   - Registration: `passkey-register-button`, `passkey-register-status-message`, `passkey-registration-placeholder`
   - Authentication: `passkey-verification-button`, `passkey-verification-status-message`, `passkey-verification-placeholder`
5. **Template structure:** Must include `<template id="...-available-template">` and `<template id="...-unavailable-template">` elements
6. **Try/except import:** `PasskeyManagementView` handles missing `django_otp_webauthn` gracefully
7. **Email notifications:** Triggered via Django signals on credential creation/deletion
8. **Admin inline:** Staff can view passkey metadata but cannot add passkeys through admin (security requirement)

### Email Notifications
| Event | Email Subject | Trigger |
|-------|---------------|---------|
| Passkey registered | "New Passkey Registered - TTStats" | WebAuthnCredential post_save (created=True) |
| Passkey deleted | "Passkey Removed - TTStats" | PasskeyManagementView POST (before delete) |

Both emails include:
- Device name
- Link to passkey management page
- Security warning if not authorized
- Suggestion to contact support

### Admin Interface
Staff can view passkey information in User admin:
- **List view:** Shows passkey count per user
- **Edit view:** Inline showing passkey name, created date, sign count
- **Can delete:** Staff can remove passkeys if needed (e.g., lost device)
- **Cannot add:** Passkeys must be registered through the UI, not admin

### Future Enhancements
1. **Recovery codes:** Generate backup codes if user loses device
2. **TOTP support:** Add authenticator app 2FA
3. **Passkey-only accounts:** Allow users to disable password entirely
4. **Device management:** Show last used date, device type detection
5. **Browser extension support:** Test with 1Password, Bitwarden passkey managers
6. **Usage analytics:** Track which auth method users prefer

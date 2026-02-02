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

# Management Commands
cd ttstats && python manage.py recalculate_elo                  # Recalculate all Elo ratings
cd ttstats && python manage.py recalculate_elo --dry-run        # Preview Elo changes

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
│   │   ├── models.py                 # Database models (9 models)
│   │   ├── views.py                  # View classes (26 views, ~1796 lines)
│   │   ├── forms.py                  # Form definitions (7 forms)
│   │   ├── elo.py                    # Elo rating calculation system
│   │   ├── urls.py                   # URL routing
│   │   ├── signals.py                # Django signals
│   │   ├── managers.py               # Custom QuerySet managers
│   │   ├── emails.py                 # Email utilities
│   │   ├── admin.py                  # Django admin config
│   │   ├── context_processors.py     # Template context
│   │   ├── management/commands/      # Management commands (recalculate_elo)
│   │   ├── migrations/               # Database migrations (16 total)
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
│   │       ├── test_context_processors.py  # Context processor tests
│   │       ├── test_passkey_views.py       # Passkey view logic tests
│   │       ├── test_passkey_integration.py # Passkey integration tests
│   │       ├── test_passkey_emails.py      # Passkey email notification tests
│   │       ├── test_passkey_admin.py       # Passkey admin interface tests
│   │       ├── test_commands.py            # Management command tests
│   │       ├── test_elo.py                 # Elo rating calculation tests
│   │       ├── test_match_list_performance.py  # Performance optimization tests
│   │       └── test_scheduled_match_conversion.py  # Scheduled match conversion tests

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
- **Factories:** factory-boy (`conftest.py` has `UserFactory`, `PlayerFactory`, `LocationFactory`, `TeamFactory`, `MatchFactory`, `GameFactory`, `ScheduledMatchFactory`)
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
| `elo.py` | `test_elo.py` | Elo calculation formulas, K-factors, doubles averaging |
| `management/commands/` | `test_commands.py` | Management command execution, output validation |

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
PlayerFactory(name="...", with_user=True)        # with_user=True creates and links a User
LocationFactory(name="...")
TeamFactory(players=[p1])                        # Creates team with 1+ players
MatchFactory(player1=p1, player2=p2, best_of=5)  # Backward-compatible, creates 1-player teams
MatchFactory(team1_players=[p1,p2], team2_players=[p3,p4], is_double=True)  # Doubles match
MatchFactory(confirmed=True)                     # Auto-confirms match after creation
GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
ScheduledMatchFactory(player1=p1, player2=p2, scheduled_date=date, scheduled_time=time)
```

Key fixtures:
- `complete_match` - Finished singles match (3-0 player1 wins, best of 5)
- Helper functions: `confirm_match(match)`, `confirm_match_silent(match)`, `confirm_team(team, match)`

### Known Gotchas

1. **Team-based architecture.** Matches now use Team model (not direct player references). Singles = 1-player teams, doubles = 2-player teams. Use `player1`/`player2` kwargs in MatchFactory for backward compatibility, or `team1_players`/`team2_players` for explicit control.
2. **MatchManager filters by current user.** In view tests, `Match.objects.get(pk=...)` only returns matches the logged-in user can see. A regular user can't see matches they're not in — `get_object_or_404(Match, pk=pk)` returns 404, not 403.
3. **Signals fire on User creation.** Every `UserFactory()` call creates a `UserProfile` with a verification token via signal. You don't need to create profiles manually.
4. **Game.save() triggers Match.save().** Creating enough games automatically sets the match winner. Tests that check "no winner yet" must not create too many games.
5. **Match confirmations use junction table.** Singles require 2 confirmations (both players), doubles require 4 (all players). Use `confirm=True` in MatchFactory or `confirm_match()` helper.
6. **Elo updates on confirmation.** Elo ratings only change when match is fully confirmed. Use `confirm_match()` or `confirm_match_silent()` in tests to trigger Elo calculation.
7. **Manager tests need thread-local manipulation.** Import `_thread_locals` from `ttstats.middleware` and set/clear `_thread_locals.user` directly. Use an `autouse` fixture to clean up.
8. **base.html requires user.player.pk.** Any view test where the user has no Player profile will crash during template rendering with `NoReverseMatch`. Always create a player for the test user.
9. **Email backend in tests.** Dev settings use `console.EmailBackend`. pytest-django's `mailoutbox` fixture or `django.core.mail.outbox` works for asserting sent emails.

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

2. **Match lifecycle (singles with Elo):**
   POST create match -> POST add game 1 -> POST add game 2 -> POST add game 3 (triggers winner) -> assert match complete, winner set, confirmation emails sent -> POST confirm as player1 -> POST confirm as player2 -> assert match_confirmed is True -> assert Elo ratings updated -> GET leaderboard -> assert stats reflect the match.

3. **Doubles match lifecycle:**
   POST create match (is_double=True, 4 players) -> POST add games -> assert winner set -> POST confirm as all 4 players -> assert match fully confirmed -> assert Elo updated for all 4 players.

4. **Scheduled match conversion flow:**
   POST schedule match -> assert emails sent -> GET calendar -> assert match appears -> GET scheduled match detail -> POST convert to match -> POST add games -> POST confirm -> assert linked match is confirmed -> GET calendar -> assert shows as converted and confirmed.

5. **Head-to-head with data:**
   Create two players, play multiple confirmed matches between them -> GET head-to-head with both player IDs -> assert all stats (game wins, margins, streaks) are calculated correctly.

These integration tests simulate real user sessions. They catch regressions where individual units pass but the wiring between them breaks (wrong redirect URL, missing context variable, signal not firing in the right order, etc.).

**When adding a new feature**, also add an integration test covering its primary happy path alongside the unit tests.


---

## Database Models (9 Total)

### Location
```python
# Fields: name, address, notes, created_at
# Ordering: by name
# Purpose: Physical location where matches are played
```

### Player
```python
# Fields: user (optional OneToOne), name, nickname, playing_style, notes, created_at,
#         elo_rating, elo_peak, matches_for_elo
# playing_style choices: normal, hard_rubber, unknown
# Properties: win_rate
# Methods: user_can_edit(user)
# Manager: PlayerManager (all visible, editable_by() for filtering)
# Purpose: Individual player profile with Elo tracking
```

### Team
```python
# Fields: players (ManyToMany), name
# Methods: __str__() (auto-generates "Player1 and Player2" format)
# save() auto-generates name from player list if blank
# Purpose: Support singles (1 player) and doubles (2 players)
# Note: Automatically created/reused when matches are created
```

### Match
```python
# Fields: is_double, team1, team2, date_played, location, match_type, best_of,
#         winner, confirmations (ManyToMany through MatchConfirmation),
#         notes, created_at, updated_at
# match_type choices: casual, practice, tournament
# best_of choices: 3, 5, 7
# Properties: team1_score, team2_score, team1_confirmed, team2_confirmed, match_confirmed,
#             player1, player2 (backward-compatible)
# Methods: user_can_edit(user), user_can_view(user), should_auto_confirm(),
#          get_unverified_players()
# save() auto-determines winner from games when match already exists (has pk)
# Manager: MatchManager (row-level security based on user)
# Purpose: Completed or in-progress match (singles or doubles)
```

### MatchConfirmation
```python
# Fields: match, player, confirmed_at
# Constraint: unique_together (match, player)
# Purpose: Junction table for match confirmations (supports doubles with 4 players)
# Note: Singles require 2 confirmations, doubles require 4
```

### Game
```python
# Fields: match, game_number, team1_score, team2_score, winner, duration_minutes
# Constraint: unique (match, game_number)
# save() auto-determines winner from scores, then calls match.save() to update match winner
# Manager: GameManager (filters by match visibility)
# Purpose: Individual game within a match
```

### EloHistory
```python
# Fields: match, player, old_rating, new_rating, rating_change, k_factor, created_at
# Constraint: unique_together (match, player)
# Purpose: Track Elo rating changes per match for transparency
# Display: Shown in match detail view
```

### UserProfile
```python
# Fields: user (OneToOne), email_verified, email_verification_token,
#         email_verification_sent_at, created_at
# Auto-created via signal when User is created (with verification token)
# Methods: create_verification_token(), verify_email(token)
# Purpose: Extended user profile for email verification
```

### ScheduledMatch
```python
# Fields: team1, team2, scheduled_date, scheduled_time, location, notes,
#         created_at, created_by, notification_sent, match (OneToOne link)
# Properties: scheduled_datetime, player1, player2 (backward-compatible),
#             is_converted, is_fully_confirmed
# Methods: user_can_view(user), user_can_edit(user) (delegates to user_can_view)
# Manager: ScheduledMatchManager (row-level security based on user)
# Ordering: by scheduled_date, scheduled_time
# Purpose: Future scheduled match with conversion to Match tracking
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
| GET | `/pingpong/leaderboard/` | LeaderboardView | Player rankings (by Elo) |
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
| GET | `/pingpong/matches/` | MatchListView | List matches (optimized) |
| GET/POST | `/pingpong/matches/add/` | MatchCreateView | Create match (singles/doubles) |
| GET | `/pingpong/matches/<id>/` | MatchDetailView | Match details + Elo changes |
| GET/POST | `/pingpong/matches/<id>/edit/` | MatchUpdateView | Edit match (limited if complete) |
| POST | `/pingpong/match/<id>/confirm/` | match_confirm | Confirm participation |
| POST | `/pingpong/matches/<match_id>/add-game/` | GameCreateView | Add game to match |
| GET/POST | `/pingpong/matches/schedule/` | ScheduledMatchCreateView | Schedule future match |
| GET | `/pingpong/scheduled-matches/<id>/` | ScheduledMatchDetailView | View scheduled match |
| GET/POST | `/pingpong/scheduled-matches/<id>/convert/` | ScheduledMatchConvertView | Convert to played match |

### Teams (all LoginRequired)
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET | `/pingpong/teams/` | TeamsListView | List all teams |
| GET | `/pingpong/teams/<id>/` | TeamDetailView | Team stats and match history |
| GET/POST | `/pingpong/teams/<id>/edit/` | TeamUpdateView | Edit team name |

## Forms Reference (`pingpong/forms.py` - 7 Total)

| Form | Model | Fields | Validation |
|------|-------|--------|------------|
| `MatchForm` | Match | is_double, player1, player2, player3, player4, date_played, location, match_type, best_of, notes | All players different; correct count for singles/doubles |
| `MatchEditForm` | Match | location, notes | (completed matches only) |
| `TeamEditForm` | Team | name | (edit team name only) |
| `GameForm` | Game | game_number, team1_score, team2_score, duration_minutes | No ties; win by 2 at deuce (>=10-10) |
| `PlayerRegistrationForm` | User | username, email, password1, password2, full_name, nickname, playing_style | Creates User + Player on save(commit=True) |
| `ScheduledMatchForm` | ScheduledMatch | player1, player2, scheduled_date, scheduled_time, location, notes | player1 != player2; date >= today |
| `MatchConvertForm` | Match | is_double, player1, player2, player3, player4, date_played, location, match_type, best_of, notes | Pre-fills from scheduled match; locks players for non-staff |

## Signals (`pingpong/signals.py`)

| Signal | Trigger | Action |
|--------|---------|--------|
| `create_user_profile` | User post_save (created=True) | Creates UserProfile + verification token |
| `track_match_winner_change` | Match pre_save | Sets `_winner_just_set` flag if winner goes from None to set |
| `handle_match_completion` | Match post_save | If `_winner_just_set`: auto-confirm (unverified) OR send confirmation emails (verified), update Elo if fully confirmed |
| `update_elo_on_confirmation` | Match post_save | If match becomes fully confirmed, calculate and update Elo ratings |
| `update_elo_on_match_confirmation` | MatchConfirmation post_save (created=True) | Triggers Elo update when individual player confirms |
| `notify_passkey_registered` | WebAuthnCredential post_save (created=True) | Sends email notification when new passkey is registered |

## Business Logic

### Team-Based Architecture
- **Singles matches:** Automatically create/reuse 1-player teams
- **Doubles matches:** Automatically create/reuse 2-player teams
- **Team reuse logic:** If exact player composition exists, reuse that team (avoids duplicates)
- **Backward compatibility:** Match.player1/player2 properties return first player from each team
- **UI:** Forms show is_double toggle + player1/player2 (+ player3/player4 for doubles)

### Match Confirmation System
1. Match created with no confirmations (MatchConfirmation junction table)
2. When winner is set for the first time (via Game.save() -> Match.save()):
   - If any player is unverified (no user or email_verified=False): auto-confirm all via DB inserts
   - If all players are verified: send confirmation emails to each
3. **Singles:** Requires 2 confirmations (both players)
4. **Doubles:** Requires 4 confirmations (all 4 players)
5. Match shows confirmation badges in UI (team1_confirmed, team2_confirmed properties)
6. **Elo trigger:** Elo ratings only update when match is fully confirmed

### Elo Rating System (`pingpong/elo.py`)
**Formula:** Traditional Elo with table tennis-specific adjustments

**Features:**
- **Singles:** Individual Elo vs Elo
- **Doubles:** Team average Elo used for probability calculation
- **K-factor base:** 32 for regular matches
- **Tournament multiplier:** 1.5x (K=48 for tournaments)
- **Best-of multipliers:** BO3=0.9x, BO5=1.0x, BO7=1.1x
- **New player boost:** 1.5x K-factor for first 20 matches

**Tracking:**
- `Player.elo_rating` (current rating, starts at 1500)
- `Player.elo_peak` (all-time highest rating)
- `Player.matches_for_elo` (counter for new player boost)
- `EloHistory` records every rating change (match, player, old, new, change, k_factor)

**Display:**
- Leaderboard sorted by Elo
- Match detail shows Elo changes per player
- Player detail shows current rating, peak, and history

**Management command:**
```bash
python manage.py recalculate_elo       # Recalculate all Elo ratings from scratch
python manage.py recalculate_elo --dry-run  # Preview changes without saving
```

### Winner Determination
- **Game:** Automatically determined by comparing team1_score vs team2_score in Game.save()
- **Match:** Automatically determined in Match.save() when a team has >= (best_of // 2 + 1) game wins

### Scheduled Match Conversion
1. User creates scheduled match (future date/time)
2. Both teams receive email notification
3. Match appears in calendar view with "Not converted" status
4. User navigates to scheduled match detail page
5. Clicks "Convert to Match" button
6. Form pre-fills with scheduled match data (players, location, notes)
7. User adds date_played, match_type, best_of
8. On save: creates Match, links via ScheduledMatch.match field
9. Calendar now shows "Converted" status
10. is_fully_confirmed property checks if linked match is confirmed

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

### Performance Optimizations (MatchListView)
**Problem:** N+1 queries rendering match lists (team scores, player lists, confirmations)

**Solution:** Extensive prefetching + Python caching
- `prefetch_related('team1__players', 'team2__players', 'games', 'confirmations')`
- Cache properties: `cached_team1_score`, `cached_team2_score`, `cached_team1_players`, `cached_team2_players`, `cached_match_confirmed`
- Reduces database queries by 90%+ on match list pages

**Testing:** `test_match_list_performance.py` validates query count stays under limits

## Templates

### Base Template Constraint
`base.html` line 169 unconditionally renders `{% url 'pingpong:player_detail' user.player.pk %}`. This means **every authenticated user must have a linked Player profile** or the page will crash with `NoReverseMatch`.  This affects both production and test code. 

### Key Templates
| Template | Purpose |
|----------|---------|
| `base.html` | Base layout with Tailwind (shadcn/ui colors), nav, alerts |
| `dashboard.html` | Stats overview |
| `match_list.html` | Match table/cards (optimized rendering) |
| `match_detail.html` | Full match view with games + Elo changes |
| `match_form.html` | Create/edit match (singles/doubles toggle) |
| `player_list.html` | Player listing |
| `player_detail.html` | Player profile & stats (Elo, peak, history) |
| `game_form.html` | Score entry |
| `head_to_head.html` | Player comparison with charts (singles only) |
| `leaderboard.html` | Rankings table (sorted by Elo) |
| `calendar.html` | Calendar view with scheduled/past matches |
| `scheduled_match_form.html` | Schedule a future match |
| `scheduled_match_detail.html` | View scheduled match details |
| `scheduled_match_convert.html` | Convert scheduled match to played match |
| `team_list.html` | List all teams |
| `team_detail.html` | Team stats and match history |
| `team_form.html` | Edit team name |

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

**Core Framework:**
```
Django==6.0
asgiref==3.11.0
sqlparse==0.5.5
```

**Database & Production:**
```
psycopg2-binary  # PostgreSQL driver
gunicorn         # WSGI server (production)
whitenoise==6.11.0
```

**Authentication:**
```
django-otp==1.5.4
django-otp-webauthn==0.3.0
```

**Testing:**
```
pytest==8.3.4
pytest-django==4.9.0
pytest-cov
coverage==7.13.1
factory-boy==3.3.1
faker==33.3.1
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
| Modify Elo calculations | `pingpong/elo.py` |
| Add management command | `pingpong/management/commands/` |
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

Current migrations (16 total):
1. `0001_initial` - Initial schema
2. `0002-0005` - Location and best_of field adjustments
3. `0006` - Match confirmation fields (old boolean approach)
4. `0007` - UserProfile model
5. `0008_teams` - **Major:** Introduced Team model
6. `0009_match_winner_confirmations` - **Major:** Refactored confirmations to MatchConfirmation junction table
7. `0010_cleanup_games` - **Major:** Renamed player scores to team scores in Game
8. `0011` - MatchConfirmation constraints
9. `0012` - ScheduledMatch model
10. `0013_scheduledmatch_to_teams` - **Major:** Migrated ScheduledMatch to Teams
11. `0014` - **Major:** Added Elo fields (elo_rating, elo_peak, matches_for_elo, EloHistory)
12. `0015` - MatchConfirmation metadata cleanup
13. `0016_scheduledmatch_match_link` - Added conversion tracking to ScheduledMatch

## Management Commands

### recalculate_elo
**Purpose:** Recalculate all Elo ratings from scratch based on confirmed match history.

**Usage:**
```bash
cd ttstats
python manage.py recalculate_elo              # Recalculate and save
python manage.py recalculate_elo --dry-run    # Preview changes without saving
```

**Process:**
1. Resets all players to 1500 Elo, 0 matches_for_elo
2. Deletes all EloHistory records
3. Processes all confirmed matches in chronological order
4. Applies same calculation logic as real-time updates
5. Shows summary of rating changes

**When to use:**
- After fixing Elo calculation bugs
- After data migrations that affect match history
- To verify Elo consistency
- Testing new K-factor or formula changes

**File:** `pingpong/management/commands/recalculate_elo.py`

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

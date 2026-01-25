# CLAUDE.md - TTStats Project Documentation

## Quick Reference

- **Project:** TTStats (Table Tennis Stats Tracker)
- **Stack:** Django 6.0, PostgreSQL 16, Tailwind CSS, Docker
- **Python Version:** 3.12
- **Main App:** `ttstats/pingpong/`

## Common Commands

```bash
# Development
docker compose -f compose.dev.yml up --build       # Start dev environment
docker compose -f compose.dev.yml exec web python manage.py migrate  # Run migrations
docker compose -f compose.dev.yml exec web python manage.py createsuperuser
docker compose -f compose.dev.yml exec web python manage.py test     # Run tests

# Testing
cd ttstats && python manage.py test pingpong       # Run all tests
cd ttstats && coverage run manage.py test pingpong # Run with coverage
cd ttstats && coverage report                       # View coverage report

# Production
docker compose -f compose.prod.yml up --build -d
```

## Project Structure

```
/home/user/ttstats/
├── ttstats/                          # Django project root
│   ├── manage.py                     # Django CLI
│   ├── pingpong/                     # Main application
│   │   ├── models.py                 # Database models (5 models)
│   │   ├── views.py                  # View classes (14 views, ~860 lines)
│   │   ├── forms.py                  # Form definitions (3 forms)
│   │   ├── urls.py                   # URL routing
│   │   ├── signals.py                # Django signals
│   │   ├── managers.py               # Custom QuerySet managers
│   │   ├── emails.py                 # Email utilities
│   │   ├── admin.py                  # Django admin config
│   │   ├── context_processors.py     # Template context
│   │   ├── migrations/               # Database migrations (7 total)
│   │   ├── templates/pingpong/       # Django templates
│   │   ├── templates/registration/   # Auth templates
│   │   ├── static/pingpong/icons/    # SVG icons (800+)
│   │   └── tests/                    # Test suite
│   │       ├── test_models.py        # Model tests (~530 lines)
│   │       └── test_signals.py       # Signal tests (~179 lines)
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
├── requirements.txt                  # Python dependencies
└── .coveragerc                       # Coverage config
```

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
# Methods: user_can_edit(user), is_linked_to_user()
# Manager: PlayerManager (all visible, editable_by() for filtering)
```

### Match (`pingpong/models.py`)
```python
# Fields: player1, player2, date_played, location, match_type, best_of, winner,
#         player1_confirmed, player2_confirmed, notes, created_at, updated_at
# match_type choices: casual, practice, tournament
# best_of choices: 3, 5, 7
# Methods: user_can_edit(user), user_can_view(user), is_complete(), update_winner()
# Manager: MatchManager (row-level security based on user)
```

### Game (`pingpong/models.py`)
```python
# Fields: match, game_number, player1_score, player2_score, winner, duration_minutes
# Constraint: unique game_number per match
# Auto-determines winner from scores
# Manager: GameManager (filters by match visibility)
```

### UserProfile (`pingpong/models.py`)
```python
# Fields: user (OneToOne), email_verified, email_verification_token,
#         email_verification_sent_at, created_at
# Auto-created via signal when User is created
```

## URL Routes

### Authentication
| Method | URL | View | Description |
|--------|-----|------|-------------|
| POST | `/accounts/login/` | CustomLoginView | User login |
| GET/POST | `/pingpong/signup/` | PlayerRegistrationView | User registration |
| GET | `/pingpong/verify-email/<token>/` | EmailVerifyView | Email verification |
| POST | `/pingpong/resend-verification/` | EmailResendVerificationView | Resend token |
| POST | `/accounts/logout/` | Django built-in | Logout |

### Core Pages
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET | `/pingpong/` | DashboardView | Main dashboard |
| GET | `/pingpong/leaderboard/` | LeaderboardView | Player rankings |
| GET | `/pingpong/head-to-head/` | HeadToHeadStatsView | Player comparison |

### Players
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET | `/pingpong/players/` | PlayerListView | List all players |
| GET | `/pingpong/players/add/` | PlayerCreateView | Create player form |
| GET | `/pingpong/players/<id>/` | PlayerDetailView | Player details |
| GET/POST | `/pingpong/players/<id>/edit/` | PlayerUpdateView | Edit player |

### Matches
| Method | URL | View | Description |
|--------|-----|------|-------------|
| GET | `/pingpong/matches/` | MatchListView | List matches |
| POST | `/pingpong/matches/add/` | MatchCreateView | Create match |
| GET | `/pingpong/matches/<id>/` | MatchDetailView | Match details |
| GET/POST | `/pingpong/matches/<id>/edit/` | MatchUpdateView | Edit match |
| POST | `/pingpong/match/<id>/confirm/` | match_confirm | Confirm participation |

### Games
| Method | URL | View | Description |
|--------|-----|------|-------------|
| POST | `/pingpong/matches/<match_id>/add-game/` | GameCreateView | Add game to match |

## Views Reference (`pingpong/views.py`)

| Line | View | Type | Purpose |
|------|------|------|---------|
| ~50 | `DashboardView` | TemplateView | Main dashboard with stats |
| ~100 | `PlayerListView` | ListView | Paginated player list |
| ~130 | `PlayerDetailView` | DetailView | Player stats & history |
| ~200 | `PlayerCreateView` | CreateView | Create new player |
| ~230 | `PlayerUpdateView` | UpdateView | Edit player profile |
| ~280 | `MatchListView` | ListView | List all visible matches |
| ~320 | `MatchDetailView` | DetailView | Match details with games |
| ~380 | `MatchCreateView` | CreateView | Create new match |
| ~450 | `MatchUpdateView` | UpdateView | Edit existing match |
| ~520 | `GameCreateView` | CreateView | Add game scores |
| ~580 | `LeaderboardView` | TemplateView | Player rankings |
| ~650 | `HeadToHeadStatsView` | TemplateView | Compare two players |
| ~720 | `PlayerRegistrationView` | CreateView | User signup |
| ~780 | `CustomLoginView` | LoginView | Custom login logic |
| ~820 | `EmailVerifyView` | View | Email verification |
| ~850 | `match_confirm` | Function | Confirm match participation |

## Forms Reference (`pingpong/forms.py`)

### MatchForm
- Validates player1 != player2
- Links to Match model

### GameForm
- Validates table tennis scoring rules
- No ties allowed
- Must win by 2 when score >= 10

### PlayerRegistrationForm
- Combined User + Player creation
- Generates email verification token
- Auto-creates Player record linked to User

## Signals (`pingpong/signals.py`)

| Signal | Trigger | Action |
|--------|---------|--------|
| `create_user_profile` | User post_save | Creates UserProfile |
| `handle_match_completion` | Match post_save | Sends confirmation emails |
| Auto-confirm logic | Match post_save | Auto-confirms for unverified emails |

## Templates

### Base Template (`templates/pingpong/base.html`)
- Tailwind CSS configuration (shadcn/ui color scheme)
- Navigation header
- Message alerts (auto-dismiss after 5s)
- Mobile-responsive design

### Key Templates
| Template | Lines | Purpose |
|----------|-------|---------|
| `base.html` | ~400 | Base layout with Tailwind |
| `dashboard.html` | ~150 | Stats overview |
| `match_list.html` | ~368 | Match table/cards |
| `match_detail.html` | ~364 | Full match view |
| `match_form.html` | ~462 | Create/edit match |
| `player_list.html` | ~238 | Player listing |
| `player_detail.html` | ~386 | Player profile & stats |
| `game_form.html` | ~266 | Score entry |
| `head_to_head.html` | ~536 | Player comparison |
| `leaderboard.html` | ~200 | Rankings table |

## Business Logic

### Match Confirmation System
1. Match created with both confirmations = False
2. If player has verified email: send confirmation email
3. If player has unverified email: auto-confirm
4. Match shows confirmation badges in UI

### Winner Determination
- **Game:** Automatically determined by comparing scores
- **Match:** Automatically determined when player wins majority of games (best_of/2 + 1)

### Row-Level Security
- `CurrentUserMiddleware` stores user in thread-local
- `MatchManager` filters: staff see all, users see only their matches
- `GameManager` inherits filtering from parent match

### Email Verification Flow
1. User registers with email
2. Verification token generated (UUID)
3. Email sent with verification link
4. User clicks link -> verified & auto-logged in
5. Unverified users cannot log in

## Testing

### Test Files
- `tests/test_models.py` - Model creation, validation, permissions
- `tests/test_signals.py` - Signal handlers, email sending

### Run Tests
```bash
cd ttstats
python manage.py test pingpong                      # All tests
python manage.py test pingpong.tests.test_models    # Model tests only
python manage.py test pingpong.tests.test_signals   # Signal tests only
coverage run manage.py test && coverage report      # With coverage
```

## Environment Configuration

### Development (`.env.dev`)
- `DEBUG=True`
- SQLite database
- Console email backend
- `DJANGO_SETTINGS_MODULE=ttstats.settings.dev`

### Production (`.env.prod`)
- `DEBUG=False`
- PostgreSQL database
- Mailgun email backend
- HTTPS enforcement
- WhiteNoise static serving
- `DJANGO_SETTINGS_MODULE=ttstats.settings.prod`

## Docker

### Development
```bash
docker compose -f compose.dev.yml up --build
# Access at http://localhost:8000
```

### Production
```bash
docker compose -f compose.prod.yml up --build -d
# Runs with Gunicorn (3 workers)
```

### Entrypoint Script (`docker/django/entrypoint.sh`)
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
1. SSH to VPS
2. Pull latest code
3. Rebuild and restart containers

## Dependencies (`requirements.txt`)

```
Django==6.0
asgiref==3.11.0
coverage==7.13.1
sqlparse==0.5.5
whitenoise==6.11.0
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
| Add tests | `pingpong/tests/` |
| Modify settings | `ttstats/settings/` |
| Modify middleware | `ttstats/middleware.py` |

## Migration Commands

```bash
cd ttstats
python manage.py makemigrations pingpong    # Create migration
python manage.py migrate                     # Apply migrations
python manage.py showmigrations              # List migrations
```

Current migrations (7 total):
1. `0001_initial` - Initial schema
2. `0002-0004` - Location field adjustments
3. `0005` - best_of field adjustment
4. `0006` - Match confirmation fields
5. `0007` - UserProfile model

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
npm run build                                      # Vendor JS + fonts, compile CSS
npm run watch:css                                  # Rebuild CSS on template change
npm run icons                                      # Rebuild the inlined Lucide sprite (output is COMMITTED)
python scripts/check_css_coverage.py               # Classes used in templates but absent from the build

# End-to-end (WebKit at iPhone size; see frontend-verification skill)
npm run test:e2e                                   # All projects
npm run test:e2e -- --project=iphone-safari        # One project
npm run test:e2e:ui                                # Interactive debugging
npm run perf                                       # Throttled page-speed report

# Development
docker compose -f compose.dev.yml up --build       # Start dev environment (includes the assets watcher)
docker compose -f compose.dev.yml exec web python manage.py migrate
docker compose -f compose.dev.yml exec web python manage.py createsuperuser

# Testing (always use pytest, never Django's manage.py test)
cd ttstats && python -m pytest --tb=short -q          # Run all tests
cd ttstats && python -m pytest ttstats/pingpong/tests/test_models.py  # Single file
cd ttstats && python -m pytest -k "TestMatch"         # Run by name pattern
cd ttstats && coverage run -m pytest && coverage report   # or ../scripts/run_tests.sh

# Management Commands
cd ttstats && python manage.py recalculate_ratings [--dry-run]   # Rebuild all ratings
cd ttstats && python manage.py generate_vapid_keys              # Web push keypair (run once, then set in .env.prod)
cd ttstats && python manage.py rebuild_point_rollups [--check]  # Rebuild (or only verify) point rollups from the raw log
cd ttstats && python manage.py point_storage --by-club          # Point-log row counts, growth rate, retention threshold
cd ttstats && python manage.py cache_control --stats|--clear|--test
cd ttstats && python manage.py warm_cache                       # Pre-populate common caches
cd ttstats && python manage.py run_scheduled_jobs [--job X] [--dry-run|--list]  # What host cron runs daily

# Production
docker compose -f compose.prod.yml up --build -d
```

---

## Testing Strategy & Rules

**This section is mandatory. Follow these rules for ALL test-related work.**

- **Framework:** pytest (`pytest.ini` at project root), `DJANGO_SETTINGS_MODULE = ttstats.settings.test`, `pythonpath = ttstats`.
- **NEVER** use Django's `TestCase` or `manage.py test`.
- When adding source code, **always create or update the corresponding test file** (`test_<module>.py`).
- Write the failing test first, then the code. Run the full suite before calling work done.
- `@pytest.mark.django_db` on test classes (or functions). Group tests in `Test<Subject>` classes, plain `assert`, helper functions instead of `setUp`.
- Create data with the `conftest.py` factories, never raw `Model.objects.create()` (except when testing the ORM itself).
- Test each layer directly: forms with `data={}`, views with the test `Client`, managers by setting the thread-local user, middleware and context processors with `RequestFactory`, emails via `mail.outbox`.
- Pure decision modules (`match_state.py`, `live_scoring.py`, `point_stats.py`, `championship_scheduling.py`, `player_hues.py`, `seasons.py`, `glicko2.py`) have no Django imports. Test them without a DB.

### Factory Reference (`conftest.py`)

```python
UserFactory(username="...", is_staff=True, ...)  # create_user(), password="testpass123"
PlayerFactory(name="...", with_user=True)        # Also creates and links a User
LocationFactory(name="...")
MatchFactory(player1=p1, player2=p2, best_of=5)  # Singles
MatchFactory(team1_players=[p1,p2], team2_players=[p3,p4], is_double=True)  # Doubles match
MatchFactory(confirmed=True)                     # Auto-confirmed
GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
ScheduledMatchFactory(player1=p1, player2=p2, scheduled_date=date, scheduled_time=time)
ChampionshipFactory(name="...", with_entries=[[p1], [p2]], created_by=player)  # Round-robin; with_participants= aliases with_entries
LiveScorerFactory(match=m, player=p)             # Guest stream, side 2
```

`team1_players`/`team2_players` are historical names for side 1 / side 2.

Fixtures and helpers: `complete_match` (finished singles, 3-0 side 1, best of 5),
`confirm_match(match)`, `confirm_match_silent(match)`,
`create_match(side1_players, side2_players, **kwargs)`,
`live_match(shared=, started=)` (host scorer row included).

### Known Gotchas

1. **Participant-based architecture.** A match's players are `MatchParticipant(match, player, side)` rows; `side` is `Side.ONE`/`Side.TWO`. There is no Team model. Read them via `match.side1_players` / `match.side2_players` / `match.players_on(side)` / `match.all_players`, and write them via `services.set_match_sides(match, side1_players, side2_players)` -- never by touching participant rows directly. `Match.winner_side` and `Game.winner_side` are ints, not FKs; compare with `Side.ONE`. `ScheduledMatch` mirrors all of this via `ScheduledMatchParticipant`.
2. **MatchManager filters by current user.** In view tests, `Match.objects.get(pk=...)` only returns matches the logged-in user can see. A regular user can't see matches they're not in — `get_object_or_404` returns 404, not 403.
3. **Signals fire on User creation.** Every `UserFactory()` creates a `UserProfile` with a verification token. Don't create profiles manually.
4. **Game.save() triggers Match.save().** Creating enough games sets the match winner. Tests that check "no winner yet" must not create too many games.
5. **Match confirmations use a junction table.** Singles need 2 confirmations, doubles 4 -- but only *verified* players count. Use `confirmed=True` in MatchFactory or `confirm_match()`.
6. **Ratings update on confirmation only** (`confirm_match()` / `confirm_match_silent()`).
7. **Manager tests need thread-locals.** Set/clear `ttstats.middleware._thread_locals.user`; clean up in an `autouse` fixture.
8. **`base.html` requires `user.player.pk`.** Every logged-in user in a view test needs a linked Player or rendering raises `NoReverseMatch`. Use `_staff_with_player()` for staff.
9. **Emails in tests:** `mailoutbox`.
10. **LocMemCache persists between tests.** The autouse `_clear_cache` fixture clears it around each test.
11. **The `clubs` flag.** Forcing it on for the whole suite fails ~200 tests by design (pre-backfill fixtures with no club see nothing). Tenancy suites opt in per class.

### Required integration flows

When adding a feature, also add an integration test for its happy path. These must exist (check, don't infer from a `-k` count):

1. Registration -> verification -> login (`test_integration.py`)
2. Singles match lifecycle with ratings (`test_integration.py`)
3. Doubles match lifecycle (`test_integration.py`)
4. Scheduled match conversion (`test_scheduled_match_conversion.py::TestConversionIntegration`)
5. Head-to-head with data (`test_views.py::TestHeadToHead*`)
6. Club invite -> anonymous signup -> membership (`test_club_invites.py::TestAnonymousSignupThroughAnInvite`)
7. Season: confirm -> leaderboard -> rollover job -> archive (`test_integration.py::TestSeasonLifecycleIntegration`)
8. Shared scoring: lobby -> signup -> join -> both phones score -> confirm (`test_shared_scoring.py::TestSharedScoringIntegration`)

---

## Core rules

### Row-level security: two boundaries

1. **The club** (tenancy) -- whose data is this? Flag-gated on `clubs`.
2. **The user** (participation) -- which of my club's rows am I party to?

- All six managers share `ScopedManagerMixin`: no user in context = unfiltered, anonymous = empty, staff = everything in their club, regular = own rows.
- **Staff bypass the user filter. NOTHING bypasses the club filter.**
- `Model.all_objects` / `_base_manager` skip row-level filtering; wrap them in `managers.scope_to_active_club(qs)` unless already scoped transitively (see `docs/decisions/kan-24-all-objects-audit.md`).
- An authenticated user with no club sees **nothing**; no user in context (commands, migrations) sees everything.
- Details: `.claude/rules/tenancy.md`.

### Match state

- **`Match.recompute()` is the single source of truth** for `winner_side`, the score caches and `is_confirmed`. It writes via queryset `.update()` so it cannot re-enter the signal pipeline. Never set those fields by hand.
- When a winner is first set: if any player is unverified, auto-confirm all; otherwise send confirmation emails. `match_confirmed` is True when all *verified* players confirmed (empty verified set = True).
- Views filter with `Match.objects.filter(is_confirmed=True)`, not in Python.
- Championship matches can have a winner with `is_confirmed=False`: filter championship data by `winner_side__isnull=False`.
- **Ratings are Glicko-2 only.** `rating_pipeline.guarded_projection` is the one path to the arithmetic for the preview *and* the write. Ratings live only in `PlayerRating`; `EloHistory` and `RatingChange.elo_*` are frozen (see `seasons.md`).
- Scoring rules (`WIN_POINTS`, `MIN_LEAD`, valid final scores) live only in `live_scoring.py`. Never re-type scorelines.

### Templates and forms

- **`{# #}` is single-line only.** A multi-line one renders into the page, and a `{%` inside it breaks compilation. Use `{% comment %}`.
- **Never `{% url 'pingpong:player_detail' p.pk %}`**; use `{% player_link player %}`, which survives a deleted player.
- **Never hand-write `<option>` loops**; render the widget or include `pingpong/_field.html`.
- Pass data to JS with `json_script`, never `escapejs`.
- Colour goes through CSS tokens; never literal palette classes like `bg-white`. Red = an action, **amber = live right now and nowhere else**, green = confirmed.
- **Dev sends no CSP; prod does.** Interactive behaviour is only verified under `applyProdCSP` in the e2e suite.
- **UI whose hidden state is managed by JavaScript must fail closed.** pytest cannot see it; use the `frontend-verification` skill.

## Maintaining these docs

- CLAUDE.md is for what nearly every task needs. Budget: 1,500 words.
- A rule about one subsystem goes in the matching `.claude/rules/<topic>.md`.
  If none fits, create one with `paths:` frontmatter naming the files it
  governs, and add it to the index below.
- Decision records and history go in `docs/decisions/`, not in rules.
- Write the rule plus one clause of why. Incident narratives and measurements
  belong in the commit message.
- When you change code a rule describes, update the rule in the same commit.
  When you add or rename a module, update the `paths:` of the rules covering it.
- Check a rule isn't already stated elsewhere before adding it.
- `tests/test_claude_docs.py` enforces the budget, the index and that every
  `paths:` glob still matches a file. `/docs-sync` checks content against code.

## Topic rules (loaded on demand)

Subsystem details live in `.claude/rules/*.md`. Each declares `paths:` and loads
only when Claude touches matching files: `tenancy`, `clubs`, `caching`,
`match-pipeline`, `live-scoring`, `championships`, `frontend`, `push`, `offline`,
`passkeys`, `observability`, `flags`, `build-and-deploy`, `seasons`, `jobs`. Decision records are
in `docs/decisions/`; the tenancy runbook is `docs/tenancy-rollout.md`.

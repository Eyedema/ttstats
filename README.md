[![Deploy to VPS](https://github.com/Eyedema/ttstats/actions/workflows/main.yml/badge.svg)](https://github.com/Eyedema/ttstats/actions/workflows/main.yml)

# TTStats

TTStats records table tennis matches for a group of players who play
together regularly. It tracks results, ratings, seasons and championships. It is
a Django app built mobile first and can be installed as a PWA. It runs in
production at [pingpong.ubaldopuocci.org](https://pingpong.ubaldopuocci.org).

## Features

- **Singles and doubles matches**, with game-by-game scores. Every *verified*
  player in a match must confirm the result before it counts. If any player is
  unverified, the match is confirmed automatically.
- **Live scoreboard.** Score a match point by point from a phone, with undo
  and side switches. Point-level stats and momentum charts are built from the
  point log.
- **Glicko-2 ratings**, kept separately for singles and doubles and updated when a match is confirmed. They feed the
  leaderboard, the player pages and a "how ratings work" explainer.
- **Seasons.** Each season has its own leaderboard. A scheduled job rolls a
  season over and archives its final standings.
- **Championships.** Round-robin tournaments with registration, a start step
  and generated fixtures.
- **Scheduling.** Plan matches on a calendar, then turn a scheduled match
  into a real one when it is played.
- **Head-to-head stats** for any two players, plus achievements.
- **Clubs (multi-tenancy).** With the `clubs` flag on, each club's data is isolated. Members join by
  invite link, and each user can switch between their clubs.
- **Accounts.** Email verification, passkeys (WebAuthn) and rate-limited
  login and registration.
- **Notifications.** Email, plus Web Push (VAPID) once the PWA is installed.

## Tech stack

| Layer        | Choice |
|--------------|--------|
| Backend      | Django 6.0, Python 3.12, Gunicorn |
| Database     | PostgreSQL 16 in prod; SQLite (or a `DATABASE_URL`) in dev |
| Cache        | Redis 7 via `django-redis`; LocMemCache when `REDIS_URL` is unset |
| Frontend     | Server-rendered templates, htmx, Alpine.js, Chart.js, Tom Select, Tailwind CSS v3 (compiled) |
| Static files | WhiteNoise |
| Security     | `django-csp`, `django-ratelimit`, `django-otp-webauthn` |
| Monitoring   | Sentry (inert without `SENTRY_DSN`) |
| Tests        | pytest, pytest-django, factory-boy; Playwright (WebKit at iPhone size) |
| Deploy       | Docker Compose on a VPS, with images built by GitHub Actions and pushed to GHCR |

## Repository layout

```
.
├── ttstats/                  Django project root (manage.py lives here)
│   ├── ttstats/              Settings (base/dev/prod/test), URLs, middleware, Sentry
│   └── pingpong/             The application
│       ├── models.py         Club, Player, Match, Game, Season, Championship, ...
│       ├── views.py          Main views; season_views.py for the archive
│       ├── services.py       Write paths (e.g. set_match_sides)
│       ├── managers.py       Row-level security (club + user scoping)
│       ├── match_state.py    Pure decision modules: no Django imports
│       ├── live_scoring.py   ...
│       ├── glicko2.py        ...
│       ├── rating_pipeline.py  The one path to rating arithmetic
│       ├── jobs.py / scheduled_jobs.py  Scheduled-job runner and registry
│       ├── flags.py          Feature flags (fail closed)
│       ├── assets/app.css    Tailwind source
│       ├── templates/        Django templates
│       ├── management/commands/
│       └── tests/            pytest suite
├── tests/e2e/                Playwright specs
├── docker/django/            Dockerfile and entrypoint
├── deploy/cron/              Host crontab for scheduled jobs
├── docs/                     Runbooks and decision records (docs/decisions/)
├── scripts/                  Asset vendoring, icon sprite, e2e server, test runner
├── .claude/rules/            Per-subsystem engineering rules
├── compose.dev.yml / compose.prod.yml
└── CLAUDE.md                 Engineering conventions (read this before contributing)
```

## Getting started

### Prerequisites

- Python 3.12
- Node.js 20, to build CSS and vendor JS, run Playwright and rebuild icons
- Docker with Compose, for Option A

### Option A: Docker

```bash
docker compose -f compose.dev.yml up --build
docker compose -f compose.dev.yml exec web python manage.py createsuperuser
```

The app is served at <http://localhost:8000>. The `assets` service runs the
Tailwind watcher, so CSS rebuilds whenever a template changes. Environment
variables come from `.env.dev`, which is committed. Anything secret goes in an
untracked `.env.dev.local`, which is loaded second and wins.

What the containers actually do:

- **The `web` container uses the production image's entrypoint.** It runs
  collectstatic, the uncommitted-migration check and `migrate`, then starts
  Gunicorn without autoreload. The `runserver` command in `compose.dev.yml` is
  ignored, because the entrypoint never passes its arguments on. After a Python
  change, restart the service (`docker compose -f compose.dev.yml restart web`).
- **Django uses SQLite, not the `db` container.** Dev settings never read
  `DB_*`, so the app uses `ttstats/db.sqlite3` through the bind mount unless
  `DATABASE_URL` is set. The `db` service still has to be running, because the
  entrypoint waits for it. `DATABASE_URL` connections require SSL, so it is
  meant for a hosted Postgres (such as the Supabase clone of prod), not the
  local container.
- **Redis is used** through `REDIS_URL`.

### Option B: Local virtualenv (SQLite)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

npm install
npm run build          # vendor JS + fonts, compile CSS (required for a styled page)

cd ttstats
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py createsuperuser
../.venv/bin/python manage.py runserver
```

Dev settings use `db.sqlite3` and print emails to the console. Run
`npm run watch:css` in another terminal while you edit templates.

Nothing loads `.env.dev*` files outside Docker. Export variables in your shell
instead, for example `export DATABASE_URL=...` to work against a hosted copy of
real data. Use the session pooler on port 5432; the transaction pooler breaks
prepared statements. Leave `REDIS_URL` unset to use LocMemCache.

To fill a separate SQLite file with fictional demo data:

```bash
cd ttstats
TTSTATS_SQLITE_NAME=$PWD/demo.sqlite3 ../.venv/bin/python manage.py migrate
TTSTATS_SQLITE_NAME=$PWD/demo.sqlite3 ../.venv/bin/python manage.py seed_demo
```

Setting the variable inline, rather than with `export`, keeps a later
`runserver` in the same shell on your normal `db.sqlite3`.

`seed_demo` and `seed_e2e` work only when `DEBUG` is on, because they create
accounts with a published password.

## Configuration

`DJANGO_ENV` selects the settings module: `prod` loads `settings/prod.py`, and
any other value loads `settings/dev.py`. `DEBUG` is fixed by the settings
module (on in dev, off in prod) and is not read from the environment. Start
production config from [`.env.prod.example`](.env.prod.example). The template
does not include the optional variables below, such as `ADMIN_URL`, `VAPID_*`
and the flag settings.

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY`, `ALLOWED_HOSTS` | Prod only, and required (dev hardcodes both) |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Postgres (prod) |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Used by the `db` container |
| `DATABASE_URL` | Dev only: use this database instead of SQLite |
| `REDIS_URL`, `REDIS_PASSWORD` | Cache; LocMemCache is used when unset |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | SMTP (prod) |
| `SITE_DOMAIN` | Prod, required. Used for links in emails and as the WebAuthn RP ID and origin, so a wrong value breaks passkeys |
| `ADMIN_URL` | Path of the Django admin (default `admin/`, must end in `/`); set a random value in prod |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_ADMIN_EMAIL` | Web Push; disabled unless both keys are set (`manage.py generate_vapid_keys`) |
| `TTSTATS_FLAGS_ON` | Comma-separated feature flags to turn on, e.g. `clubs` |
| `TENANCY_CANARY_SAMPLE_RATE` | Fraction of scoped querysets the tenancy leak canary inspects; has no effect unless the `tenancy_canary` flag is on |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_CAPTURE_4XX` | Error monitoring |

### Feature flags

The flags live in `FEATURE_FLAGS` in `settings/base.py`. Two are declared:

- `clubs` turns on club scoping. It is off by default, so a fresh local install
  has no tenancy isolation; production runs with it on.
- `tenancy_canary` turns on the leak canary.

The old `seasons` and `glicko2` flags are retired, and a stale name in
`TTSTATS_FLAGS_ON` is ignored. Turn flags on globally
with `TTSTATS_FLAGS_ON`, or for a single user with a `FeatureFlagOverride` row
in the admin. Overrides apply in both directions, so they also work as a
per-user kill switch. A flag that was never declared is off.

## Testing

Always run the Python tests with pytest, never `manage.py test`.

```bash
.venv/bin/python -m pytest --tb=short -q                       # full suite
.venv/bin/python -m pytest ttstats/pingpong/tests/test_models.py
.venv/bin/python -m pytest -k "TestMatch"
source .venv/bin/activate && cd ttstats && ../scripts/run_tests.sh   # with coverage
```

The tests use `ttstats.settings.test`. Build test data with the factories in
`conftest.py`. See [CLAUDE.md](CLAUDE.md) for the testing rules, the factory
reference and the known gotchas.

### Browser tests

pytest checks the rendered HTML but never runs any JavaScript. Behaviour that
depends on JavaScript, CSS or a mobile viewport is covered by a Playwright
suite, which runs in real WebKit at iPhone size. Its projects:

- `iphone-safari`
- `iphone-pwa` (installed app)
- `iphone-no-js`
- `iphone-reduced` (reduced motion)
- `iphone-dark`
- `desktop-chrome`

CI runs only `setup` and the first four of those; `iphone-dark` and
`desktop-chrome` run only locally. Specs that call the `applyProdCSP` helper
run under the production Content-Security-Policy, which dev does not send.

```bash
npx playwright install webkit chromium            # once (chromium is for desktop-chrome)
npm run test:e2e                                  # all projects
npm run test:e2e -- --project=iphone-safari       # one project
npm run test:e2e:ui                               # interactive
```

`scripts/e2e_server.sh` rebuilds the CSS, migrates a throwaway SQLite file and
seeds it. It also clears `DATABASE_URL`, so a test run can never reach a real
database.

### Frontend checks

```bash
python scripts/check_css_coverage.py   # classes used in templates but missing from the build
npm run icons                          # rebuild the inlined Lucide sprite (output is committed)
```

## Management commands

Run these from `ttstats/`:

| Command | What it does |
|---------|--------------|
| `recalculate_ratings [--dry-run]` | Rebuild every rating from match history |
| `run_scheduled_jobs [--job X] [--dry-run\|--list]` | Run the daily jobs (season rollover, pruning old job runs) |
| `rebuild_point_rollups [--check]` | Rebuild point rollups from the raw point log, or only verify them |
| `point_storage --by-club` | Point-log row counts, growth rate and retention threshold |
| `award_achievements` | Award achievements again from existing data |
| `backfill_clubs` | Assign club ownership to data created before clubs existed |
| `cache_control --stats\|--clear\|--test` | Inspect or clear the cache |
| `warm_cache` | Fill the most-used caches ahead of traffic |
| `generate_vapid_keys` | Generate a Web Push keypair (run once) |
| `seed_demo [--wipe]`, `seed_e2e` | Demo and e2e fixture data (DEBUG only) |

## Deployment

The [CI/CD pipeline](.github/workflows/main.yml) runs the test jobs on every
pull request and on every push to `master`. Only a push to `master` builds and
deploys.

1. **Detect changes.** Python tests run only when `.py` files changed. The
   browser tests run when Python or frontend files changed. A commit with
   neither, such as a docs-only change, still builds and deploys.
2. **Test.** Build assets, then run pytest with coverage. On a pull request,
   the coverage report is posted as a comment.
3. **E2E.** Run the CI Playwright projects (see [Browser tests](#browser-tests)).
4. **Build image.** Build `docker/django/Dockerfile` and push it to
   `ghcr.io/eyedema/ttstats-web` with two tags, the commit SHA and `latest`.
5. **Deploy.** SSH to the VPS and `git pull` the checkout in `WORK_DIR`, which
   supplies the compose and cron files. Then pull the image pinned to the
   commit, run `docker compose -f compose.prod.yml up -d`, and run `migrate`
   and `collectstatic`.

The VPS has little memory, so the image is built in CI and never on the server.
The container's entrypoint waits for Postgres, collects static files, and
refuses to start if a model change has no committed migration. It then migrates
and starts Gunicorn.

**Rollback.** Re-running an older commit's deploy job pins that commit's image.
It does not revert migrations, and it still uses the latest
`compose.prod.yml`, because the deploy pulls `master` first. If the schema has
moved on, you have to restore a database dump.

Scheduled jobs run from the host's crontab, not from the deploy. See
[`deploy/cron/README.md`](deploy/cron/README.md).

To run production by hand:

```bash
cp .env.prod.example .env.prod            # then fill it in
docker network create pangolin            # external network for the reverse proxy, if it doesn't exist
docker login ghcr.io                      # the image is private; or add --build to `up` instead
docker compose -f compose.prod.yml --env-file .env.prod up -d
```

`--env-file` matters. `REDIS_PASSWORD` is substituted into the Redis command
line from the shell or that file, not from the services' `env_file`.

## Further reading

- [CLAUDE.md](CLAUDE.md): core engineering rules (row-level security, match
  state, templates, testing)
- [`.claude/rules/`](.claude/rules/): rules for each subsystem (tenancy,
  caching, live scoring, championships, seasons, push, passkeys, ...)
- [`docs/decisions/`](docs/decisions/): decision records (Glicko-2, seasons,
  retiring Elo, point-event retention, ...)
- [`docs/tenancy-rollout.md`](docs/tenancy-rollout.md),
  [`docs/glicko2-cutover.md`](docs/glicko2-cutover.md): runbooks

---
paths:
  - "package.json"
  - "tailwind.config.js"
  - "scripts/**"
  - "docker/**"
  - "compose*.yml"
  - ".github/workflows/**"
  - "ttstats/ttstats/settings/**"
  - "requirements.txt"
  - ".env*"
---

# Build, vendoring and deployment

## Frontend build

- Tailwind v3 is **compiled**. Source `pingpong/assets/app.css` (outside
  `static/` so collectstatic never copies raw `@tailwind`), output
  `static/pingpong/css/app.css` (gitignored). Config: `tailwind.config.js`.
- **A class Tailwind cannot see is silently dropped.** `content` globs cover all
  templates *and* `pingpong/*.py` (forms.py builds class strings). Moving class
  names into a new Python module means adding it to the globs.
- Tests don't need the built CSS; the browser does (`npm run build:css`).
- Docker: the `node:20-alpine AS assets` stage builds the CSS, and its
  `COPY --from=assets` must stay **after** `COPY ttstats/ .`. `compose.dev.yml`
  bind-mounts `./ttstats`, hence the separate `assets` watcher service.
- **Static storage is `STORAGES`**, not `STATICFILES_STORAGE`. `prod.py` and
  `e2e.py` (the Playwright server: dev plus prod's static pipeline, hashed
  URLs forced under DEBUG) use WhiteNoise's manifest storage; dev and tests don't.

## Vendored JavaScript

- htmx, Alpine, Chart.js and Tom Select are served from `self`. `npm run vendor`
  copies builds into `static/pingpong/js/vendor/` and `css/vendor/` (gitignored).
  Versions are pinned in `package-lock.json`.
- **Vendored files must ship their `.map` siblings.** Manifest storage
  hard-fails `collectstatic` on a missing `sourceMappingURL` target, and
  `entrypoint.sh` runs it under `set -e`, so this breaks the deploy.

## Environments

- **Dev:** `DEBUG=True`, SQLite, console email, `ttstats.settings.dev`.
- **`DATABASE_URL` opts dev into the Supabase clone of prod** (session pooler,
  port 5432; the 6543 transaction pooler breaks Django). Tests are unaffected:
  `settings/test.py` overrides `DATABASES` after `from .dev import *`.
- **Prod:** `DEBUG=False`, PostgreSQL, Mailgun, HTTPS, WhiteNoise,
  `ttstats.settings.prod`. Production is the `db` service in `compose.prod.yml`;
  Supabase is only a dev clone.
- Optional `.env.prod` vars (see `.env.prod.example`): `VAPID_*` for push,
  `SENTRY_*` for Sentry. Unset, both are no-ops. `DB_CONN_MAX_AGE` (default 60,
  `0` = close per request) sets persistent connections, with `CONN_HEALTH_CHECKS`.
- **Gunicorn runs 3 sync workers** (`entrypoint.sh`): at most 3 persistent
  connections. Moving to gevent or threads multiplies that; revisit
  `CONN_MAX_AGE` in the same change.
- **CI/CD** (`.github/workflows/main.yml`): tests with coverage on push/PR; SSH
  deploy on master. `.py` changes gate tests, frontend extensions gate the asset
  build.
- Tenancy cutover runbook: `docs/tenancy-rollout.md`.

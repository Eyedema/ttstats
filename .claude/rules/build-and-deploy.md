---
paths:
  - "package.json"
  - "tailwind.config.js"
  - "scripts/**"
  - "docker/**"
  - "Dockerfile"
  - "compose*.yml"
  - ".github/workflows/**"
  - "ttstats/ttstats/settings/**"
  - "requirements.txt"
  - ".env*"
---

# Build, vendoring and deployment

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

## Environment & Deployment

- **Dev:** `DEBUG=True`, SQLite, Console email, `DJANGO_SETTINGS_MODULE=ttstats.settings.dev`
- **`DATABASE_URL` opts dev into the Supabase clone of prod.** Unset, `dev.py` uses the SQLite file as before; set, it parses the URL with `dj-database-url` (`ssl_require=True`). Use Supabase's **session** pooler on port 5432 -- the transaction pooler on 6543 has no prepared statements and Django breaks on it. **Tests are unaffected either way**: `settings/test.py` overrides `DATABASES` to in-memory SQLite *after* `from .dev import *`, so an exported `DATABASE_URL` can never point the suite at a real database.
- **Prod:** `DEBUG=False`, PostgreSQL, Mailgun, HTTPS, WhiteNoise, `DJANGO_SETTINGS_MODULE=ttstats.settings.prod`
- **Docker services:** web (Django), db (PostgreSQL), redis (Redis 7 Alpine)
- **Web push needs three vars in `.env.prod`** (`compose.prod.yml` already passes the whole file through): `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_ADMIN_EMAIL` (a `mailto:` URL). Until they are set the app emails as before -- push is not a hard dependency of any code path.
- **Sentry needs one var in `.env.prod`**: `SENTRY_DSN`. `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE` (the deploy's commit SHA), `SENTRY_TRACES_SAMPLE_RATE` and `SENTRY_CAPTURE_4XX` are optional and documented in `.env.prod.example`. Until the DSN is set nothing is sent and no code path changes.
- **The tenancy cutover has a runbook: `docs/tenancy-rollout.md`.** Seven steps,
  each with its own verification command, gate and rollback. Steps 5 and 6 roll
  back by flipping `TTSTATS_FLAGS_ON` -- no deploy, no migration, effective on
  the next request. Step 7 (non-null columns) is the point of no return and is
  the only one whose rollback is "restore the snapshot".
- **Production is the `db` service in `compose.prod.yml`** (PostgreSQL 16,
  volume `pgdata`). Supabase holds only a *dev clone* pulled from it by
  `pg_dump` -- it is not authoritative and is not a fallback.
- **CI/CD** (`.github/workflows/main.yml`): On push/PR runs tests with coverage; on master push deploys via SSH to VPS. The test job is gated on changed files -- **`.py` gates the test run, frontend extensions gate the asset build**, so a template-only commit still builds the CSS.
- **Migration `0028_drop_team` is one-way.** Reversing would re-add NOT NULL FK columns with nothing to put in them, so it raises a `RuntimeError` naming the remedy: restore a pre-0028 snapshot.
- **Deploy runs `collectstatic` under `set -e`** (`docker/django/entrypoint.sh`), and prod uses WhiteNoise's hashing manifest storage. A vendored file whose `sourceMappingURL` target is missing therefore **fails the deploy**, not just the page -- `scripts/vendor_assets.mjs` copies `.map` siblings for exactly this reason.

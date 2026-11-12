---
paths:
  - "deploy/**"
  - "compose.prod.yml"
  - "docker/django/entrypoint.sh"
  - ".github/workflows/main.yml"
  - "ttstats/ttstats/healthz.py"
  - "ttstats/pingpong/tests/test_healthz.py"
  - "ttstats/pingpong/tests/test_deploy_config.py"
  - "ttstats/pingpong/tests/test_deploy_script.py"
---

# Operations: health, deploys, backups (KAN-106)

Why each piece is shaped as it is: `docs/decisions/kan-106-reliability.md`.

## `/healthz` (KAN-109)

- **Answered by `HealthzMiddleware`, first in `MIDDLEWARE`** (prod.py's CSP
  insert at index 1 stays behind it; `test_healthz.py` pins both). It skips
  host validation, the HTTPS redirect, sessions, auth and tenancy, so Docker's
  plain-HTTP probe to `127.0.0.1:8000` gets the same answer as the proxy.
- **503 only when this process cannot serve**: DB `SELECT 1` failed, or a
  migration is pending. **Redis down is `degraded`, still 200**: a probe that
  drives rollbacks must not fire on a slower app.
- Cheap: one query once migrations are known clean (memoised per process;
  a pending answer is re-asked every 10 s). Body names the failed check,
  never the exception. `never_cache`, GET/HEAD only, no cookies.
- Never traced (`sentry.UNTRACED_PATHS`), never rate limited (limits are
  per-view), never answered by the service worker (`sw_routes.test.mjs`).
- Callers: the `web` healthcheck in `compose.prod.yml`, the deploy smoke test,
  the uptime monitor, and `snapshot_mode.js`'s back-online probe.

## Deploys (KAN-110)

- **The deploy is `deploy/deploy.sh <image> <sha>`**; the workflow only does
  `git pull` and calls it, so a hand run on the box is the same deploy.
  Behaviour lives in the script, tested by `test_deploy_script.py` with a
  fake `docker` (`tests/deploy_fakes/`). A new step means a new fake verb.
- **It reads the running image before changing anything** (the rollback
  target), and writes `deploy/.last-good-image` (gitignored) only on success.
- **Every compose call pins `WEB_IMAGE` and `SENTRY_RELEASE`**; never a bare
  `dcp up -d web` (falls back to `:latest`).
- **Never run jobs from the deploy** (`jobs.md`): the SSH step has a timeout.

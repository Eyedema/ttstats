# KAN-106: production reliability (backups, safe deploys, monitoring)

Production is one VPS running `compose.prod.yml` (`db`, `redis`, `web`) behind
Pangolin/Traefik. This epic makes three promises: losing the box loses at
most a day, a bad deploy rolls itself back, and anything broken reaches the
owner's phone. Rules that follow from it live in `.claude/rules/ops.md`.

## `/healthz` (KAN-109)

- **A middleware, first in the stack, not a view.** A view sits behind
  `SecurityMiddleware` (301 to https for Docker's plain-HTTP probe), host
  validation (`ALLOWED_HOSTS` is the public name; the probe says
  `127.0.0.1:8000`), sessions, auth, OTP and tenancy. Exempting a view from
  each of those is five settings that drift; answering first is one line.
  The body holds nothing a Host-header attack could use (status, release SHA).
- **What fails it:** the DB, or a pending migration. A container booted on
  an unmigrated schema must never take traffic, and the deploy migrates
  before the swap, so a pending migration on a live container means
  something skipped a step. The plan costs ~ms and a few queries, so a
  clean answer is remembered for the process (migrations cannot become
  unapplied under running code); a pending one is re-asked every 10 s.
- **What does not:** Redis. Sessions are `cached_db` and fall back to the
  database, and every cache read already tolerates a miss, so Redis down is
  slower, not down. The probe reports `degraded` with a 200, because it is
  what decides rollbacks (KAN-116) and Redis is not part of a release.
  django-redis waits 5 s and retries once on a hung server, so the ping runs
  on a daemon thread with a 1 s budget (`timeout`, still 200).
- **Healthcheck cadence:** `interval: 30s` with `start_interval: 2s` (Docker
  25+; the VPS runs 29.9). The ticket proposed 10 s; 30 s keeps gunicorn's
  access log at ~3k probe lines a day, while `start_interval` still marks a
  fresh container healthy within seconds, which is the only time a deploy
  waits on it. Docker never restarts an unhealthy container by itself here
  (no swarm); the status is for the deploy.
- No `db` healthcheck/`depends_on: service_healthy`: changing the `db`
  service's config makes the next `up -d` recreate Postgres. The
  entrypoint's `nc -z` loop stays.
- **The offline back-online probe (KAN-131) moved from `HEAD` of the
  manifest to `HEAD /healthz`.** The manifest answered 200 with the database
  down; `/healthz` answers 503, so "You're back online" now means the pages
  will actually load.

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

## Deploys (KAN-110)

- **The deploy is a committed script** (`deploy/deploy.sh`, KAN-113), first
  moved verbatim so the move could not break a deploy, then changed. The
  workflow keeps only `git pull` (so the script that runs is the new one).
- **One migrate, before the swap** (KAN-114). Before: the entrypoint ran
  collectstatic + migrate on every container start, and the workflow ran both
  again after `up`. Now a one-off `compose run` with the new image runs
  `makemigrations --check && migrate && collectstatic` while the old
  container serves, and prod's entrypoint skips them (`SKIP_BOOT_TASKS=1`).
  A failure there stops the deploy with nothing swapped. The price is the
  expand/contract rule: old code must tolerate the new schema.
  collectstatic into the shared volume is safe for the old container: names
  are content-hashed and nothing deletes the old ones (40 CSS versions on the
  VPS on 2026-11-13); the old process has its manifest in memory.
- **Smoke test through the real proxy** (KAN-116). The VPS reaches its own
  public name through Pangolin (~0.1 s), so the check is the user's path:
  TLS, Traefik, routing. `/healthz` must report the new `release` (proves the
  proxy reaches the *new* container, not a stale one) and five pages must
  answer 200: both login templates (forms, CSRF, static manifest), `/sw.js`
  and the manifest (rendered without a request), `/offline/`.
- **Rollback is the previous image, never migrations.** Exit codes separate
  "rolled back" (1) from "nothing changed" (3) and "rollback failed" (4).
  The Sentry message is sent from inside the serving container (it has the
  SDK and DSN; the host has neither), best effort: the red workflow is the
  guaranteed signal.
- The first deploy of KAN-114/116 rolls back to an image without `/healthz`
  only if it fails; that is why KAN-109 + KAN-113 shipped one deploy earlier.

## Backups (KAN-107, KAN-117, KAN-118) and the drill (KAN-108)

- **Host cron, not the job runner**: the image has no `pg_dump`; the `db`
  container has the matching 16.x. 02:45 UTC, before the 03:15 jobs, so a
  bad nightly job is always preceded by a backup.
- **Remote: Cloudflare R2** (owner, 2026-11-17), over B2: 10 GB free, no
  egress fees for a restore, S3 API so rclone's `s3` backend with
  `provider = Cloudflare`. The token is scoped to the one bucket with object
  read/write only, hence `no_check_bucket = true`.
- **age, two recipients.** The main private key lives only in the owner's
  password manager, never on the VPS: whoever takes the box gets nothing
  from the bucket that the database on the box would not already give them.
  The drill decrypts with a second, drill-only key kept on the VPS, so the
  monthly drill needs no human; a leaked drill key is no worse than the
  live database next to it.
- **Retention by count of names, not dates or mtimes**: no BSD/GNU `date`
  arithmetic, and a run of failed nights keeps older backups longer rather
  than expiring them. Only names matching the backup pattern are deleted.
- **The drill restores a dump into a throwaway Postgres; never `migrate`**
  (KAN-75: 0010 cannot apply to an empty Postgres). It compares the latest
  *pingpong* migration with prod's, row counts >= 99%, and the newest daily
  object's age (fails over 50 h); a quiet week of no matches only warns.

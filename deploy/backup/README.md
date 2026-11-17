# Off-host database backups (KAN-118) and the restore drill (KAN-108)

Two host scripts, run by host cron (`deploy/cron/ttstats.cron`), never from the
deploy workflow:

- **`backup.sh`** — nightly at 02:45 UTC. `pg_dump -Fc` inside the prod `db`
  container, verified with `pg_restore --list` (also inside the container: the
  host has no Postgres client), encrypted with [age](https://age-encryption.org)
  to one or more public keys, uploaded with [rclone](https://rclone.org), then
  rotated on the remote. Sentry cron check-ins at start and end.
- **`restore_drill.sh`** — monthly. Downloads the newest `daily/` backup,
  decrypts it with the drill-only key, restores it into a throwaway
  `postgres:16` container (`--network none`, `--memory 512m`, tmpfs data dir)
  and compares it with prod.

Both are bash, `set -euo pipefail`, log timestamped lines to stdout (cron pipes
them to `logger`), and clean up their temp dir (and the drill's container) in a
`trap`, on success or failure. Run either with `--help` for flags and exit codes.

**Status:** the scripts are tested (`ttstats/pingpong/tests/test_backup_scripts.py`,
fake `docker`/`age`/`rclone`/`curl`), but the cron lines are commented out until
the KAN-117 setup below is done.

## What lands where

```
<BACKUP_REMOTE>/daily/ttstats-20261113-0245.dump.age      every night, newest 7 kept
<BACKUP_REMOTE>/weekly/ttstats-20261115-0245.dump.age     Sundays, newest 4 kept
<BACKUP_REMOTE>/monthly/ttstats-20261201-0245.dump.age    1st of the month, newest 6 kept
<BACKUP_REMOTE>/adhoc/ttstats-20261113-1002-pre-deploy-<sha>.dump.age   --tag runs, newest 10 kept
/root/ttstats-backups/                                    the newest 2 encrypted dumps, locally
```

- Uploads go to `<name>.partial` and are renamed with `rclone moveto` only when
  complete, so an interrupted upload never looks like a backup. Stray `.partial`
  objects older than a day are deleted.
- Weekly and monthly copies are server-side copies of the daily object.
- Retention keeps the newest N **by the timestamp in the name**, never by
  mtime, and only touches names matching `ttstats-YYYYmmdd-HHMM[-tag].dump.age`.
  Being count-based, a run of failed nights keeps older backups longer instead
  of deleting them.
- **Nothing is deleted unless every upload of the run succeeded.**
- The plaintext dump only ever exists in a `mktemp -d` directory (mode 700),
  and is deleted as soon as it is encrypted.

## Exit codes

| | `backup.sh` | `restore_drill.sh` |
|---|---|---|
| 0 | ok | ok (warnings may be logged) |
| 1 | unexpected | unexpected |
| 2 | usage / config | usage / config |
| 3 | pg_dump failed | download failed, or no backup on the remote |
| 4 | verify failed (empty, or `pg_restore --list` rejected it) | decryption failed (wrong key, truncated, corrupt) |
| 5 | encryption failed | throwaway container didn't start / become ready |
| 6 | upload failed (nothing deleted) | `pg_restore` failed |
| 7 | retention failed (the new backup *is* uploaded) | a sanity check failed |
| 8 | | querying prod failed |

Any non-zero exit sends a Sentry `error` check-in (once config is loaded). A
failing check-in (Sentry down) is logged and ignored: it never fails a backup.

## Configuration

Read from `BACKUP_ENV_FILE`, default `/root/ttstats/deploy/backup/backup.env`
(gitignored; values in the file win over the environment). Every variable,
with defaults, is in [`backup.env.example`](backup.env.example). The essentials:

| Variable | Meaning |
|---|---|
| `BACKUP_REMOTE` | rclone remote + path, e.g. `b2:<bucket>/ttstats` |
| `BACKUP_AGE_RECIPIENTS` / `BACKUP_AGE_RECIPIENTS_FILE` | age public keys (main + drill) |
| `BACKUP_RCLONE_CONFIG` | rclone config file (optional) |
| `SENTRY_CRON_URL_BACKUP` / `SENTRY_CRON_URL_DRILL` | check-in URLs without `?status=` (optional) |
| `BACKUP_DRILL_IDENTITY` | the drill key's private half (drill only) |
| `BACKUP_PROJECT_DIR`, `BACKUP_COMPOSE_FILE`, `BACKUP_DB_SERVICE` | where prod runs |

## Flags

```sh
deploy/backup/backup.sh --dry-run          # dump + verify + encrypt in the temp dir; no upload,
                                           # no retention, no local copy, no check-ins. Without
                                           # recipients it encrypts to a throwaway key.
deploy/backup/backup.sh --tag pre-deploy-$(git rev-parse --short HEAD)   # -> adhoc/
deploy/backup/restore_drill.sh                     # newest daily/
deploy/backup/restore_drill.sh --file /root/ttstats-backups/ttstats-....dump.age
deploy/backup/restore_drill.sh --strict-freshness  # an old newest match fails instead of warning
deploy/backup/restore_drill.sh --skip-freshness
deploy/backup/restore_drill.sh --keep-container    # leave the restored DB up to poke at
```

## The drill's checks

1. Row counts of `pingpong_match`, `pingpong_pointevent`, `pingpong_playerrating`
   and `auth_user` are > 0 and >= 99% of prod's (prod is queried in read-only
   transactions via `docker compose exec -T db psql`).
2. The latest `pingpong` row of `django_migrations` equals prod's. (A deploy
   with a migration between the backup and the drill fails this; rerun.)
3. The newest `daily/` object's name is at most 50 h old (downloads only, not
   `--file`). This fails: it means the nightly backup stopped.
4. The newest `pingpong_match.date_played` is at most 2 days old. **Warn-only by
   default**, since a quiet fortnight is plausible and checks 1 and 3 already
   catch a stale backup; `--strict-freshness` makes it fail, `--skip-freshness`
   skips it.

The restore is `pg_restore --no-owner --no-privileges --exit-on-error` into an
empty database. **Never `manage.py migrate` a fresh Postgres to restore into**:
migration 0010 can't apply to one (KAN-75).

## KAN-117 setup checklist (manual, once)

The remote and where the main private key lives are **not decided yet**;
`<...>` marks each choice.

- [ ] Install the tools on the VPS: `apt install age rclone` (check
      `age --version` >= 1.1, `rclone version`).
- [ ] **Main key** — on a machine that is *not* the VPS:
      `age-keygen -o ttstats-backup-main.agekey`. Store the private key in
      `<password manager / offline location>`, and a second copy in
      `<second location>`. Note the public key (`age1...`). The main private
      key must never be copied to the VPS.
- [ ] **Drill key** — on the VPS:
      `install -d -m 700 /root/.config/ttstats && age-keygen -o /root/.config/ttstats/drill.agekey && chmod 600 /root/.config/ttstats/drill.agekey`.
      Note its public key. (It can decrypt backups, so it is as sensitive as
      the data already on the box, and no more.)
- [ ] **Remote** — create `<bucket>` at `<provider>` with an application key
      scoped to that bucket, ideally without delete rights beyond what the
      retention needs (or enable the provider's object lock / lifecycle as a
      second line). `rclone config` → remote `<name>`; test with
      `rclone lsd <name>:<bucket>`.
- [ ] `cp deploy/backup/backup.env.example deploy/backup/backup.env && chmod 600 deploy/backup/backup.env`
      and fill in `BACKUP_REMOTE`, `BACKUP_AGE_RECIPIENTS` (main + drill
      public keys), `BACKUP_DRILL_IDENTITY`.
- [ ] Sentry → Crons: create monitors `ttstats-backup` (daily, 02:45 UTC,
      margin 30 min, max runtime 30 min) and `ttstats-restore-drill`
      (monthly, 2nd at 04:30 UTC). Put their check-in URLs in `backup.env`.
- [ ] `deploy/backup/backup.sh --dry-run` → `dry run OK`.
- [ ] `deploy/backup/backup.sh` → `backup OK`; `rclone ls <remote>` shows it.
- [ ] `deploy/backup/restore_drill.sh` → `drill OK`.
- [ ] **Decrypt one with the main key off the VPS** (below) and `pg_restore --list` it.
      A backup nobody can decrypt is not a backup.
- [ ] Uncomment the two lines in `deploy/cron/ttstats.cron` and re-install the
      crontab (`deploy/cron/README.md`).
- [ ] After the first night: `journalctl -t ttstats-backup --since yesterday`,
      the Sentry monitor is green, record it on KAN-118.

## By hand

List and fetch:

```sh
rclone lsl <remote>:<bucket>/ttstats/daily/
rclone copyto <remote>:<bucket>/ttstats/daily/ttstats-YYYYmmdd-HHMM.dump.age ./x.dump.age
```

Decrypt and inspect (any machine with the main key, or the VPS with the drill key):

```sh
age -d -i <path/to/key> -o x.dump x.dump.age
pg_restore --list x.dump | head          # or, on the VPS (no host pg_restore):
docker compose -f compose.prod.yml exec -T db pg_restore --list < x.dump | head
```

Restoring into prod is the procedure in `docs/glicko2-cutover.md` "Backup and
restore", with a custom-format dump: stop web, then

```sh
docker compose -f compose.prod.yml exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --exit-on-error' < x.dump
```

and start web the pinned way ("Restarting web" in the same doc). Delete the
plaintext `x.dump` afterwards.

A pre-deploy snapshot: `deploy/backup/backup.sh --tag pre-deploy-<sha>`
(lands in `adhoc/`, newest 10 kept).

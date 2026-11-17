# Disaster recovery (KAN-112)

Production is one VPS (`142.132.165.247`, `/root/ttstats`) running
`compose.prod.yml` (`db`, `redis`, `web`) behind Pangolin/Traefik at
`https://pingpong.ubaldopuocci.org`. Rules behind this runbook:
`.claude/rules/ops.md`; why: `docs/decisions/kan-106-reliability.md`.

| | Target | How |
|---|---|---|
| **RPO** (data lost) | <= 24 h; <= 0 for a bad deploy | nightly backup 02:45 UTC + a tagged backup before every migrate |
| **RTO** (time down) | ~2 h for a lost VPS; ~15 min for a bad table; ~2 min for a bad deploy | sections below |

Every command assumes, on the VPS:

```bash
cd /root/ttstats
alias dcp='docker compose -f compose.prod.yml'
alias dweb='docker compose -f compose.prod.yml exec -T web python manage.py'
```

**Never `migrate` an empty database.** Migration 0010 cannot apply to a fresh
Postgres (KAN-75): a database is only ever rebuilt by restoring a dump.

## What must exist off the VPS

Check these once; without them section 1 cannot be done.

- [ ] **Password manager:** the main age private key ("TTStats backup key"),
      a copy of `/root/ttstats/.env.prod`, the R2 API token (access key id,
      secret, endpoint), the Pangolin admin login.
- [ ] **Cloudflare R2** bucket `ttstats-backups`: `daily/`, `weekly/`,
      `monthly/`, `adhoc/` under `ttstats/`.
- [ ] **GitHub:** the repo, and images `ghcr.io/eyedema/ttstats-web:<sha>`.
- [ ] **DNS** for `pingpong.ubaldopuocci.org` (A record to the VPS).

Not backed up today, rebuilt by hand in section 1: the Pangolin config
(`/root/pangolin/config`: its sqlite DB, Traefik config, certificates are
re-issued), SSH hardening, fail2ban, journald limits, swap.

## 1. The VPS is gone

1. **New box.** Hetzner, Ubuntu LTS, >= 4 GB RAM, your SSH key. Point the DNS
   A record at it (lower the TTL first if you can).
2. **Base.** `apt update && apt -y upgrade`; install Docker Engine + the
   compose plugin (docs.docker.com, "Install using the apt repository");
   `apt install -y age rclone fail2ban git`. 4 GB swap:
   `fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile`
   and add it to `/etc/fstab`. SSH: key-only root (a drop-in in
   `/etc/ssh/sshd_config.d/`), then `systemctl reload ssh`.
3. **Pangolin.** Run its installer (`/root/pangolin`), log in, create the site
   and the resource for `pingpong.ubaldopuocci.org` with target
   **`ttstats-web`, port 8000, http** (the compose alias, not a container
   name). The `pangolin` Docker network must exist before step 5.
4. **App checkout.** `git clone <repo> /root/ttstats`, `cd /root/ttstats`,
   restore `.env.prod` from the password manager (`chmod 600`).
5. **Database and cache only.** `dcp up -d db redis`; wait for
   `dcp exec -T db pg_isready` to say "accepting connections".
6. **Fetch the newest backup and decrypt it on your own machine** (the main
   key never goes to a server):

   ```bash
   # laptop, with the r2 remote configured as in deploy/backup/README.md
   rclone lsf r2:ttstats-backups/ttstats/daily/ | sort | tail -1
   rclone copyto r2:ttstats-backups/ttstats/daily/<name>.dump.age ./latest.dump.age
   age -d -i <(pbpaste) -o latest.dump latest.dump.age   # key copied from the password manager
   pg_restore --list latest.dump | head                   # sanity: a TOC, not an error
   scp latest.dump root@<new-ip>:/root/latest.dump && rm latest.dump latest.dump.age
   ```

   (An `adhoc/pre-deploy-*` backup is newer than the daily one if a deploy
   ran since: take whichever name has the latest timestamp.)
7. **Restore** into the empty database:

   ```bash
   dcp exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' < /root/latest.dump
   shred -u /root/latest.dump
   ```

8. **Web, pinned.** Pick the release that was running (the newest green
   "Deploy to VPS" run on master; its SHA tags the image). Log in to GHCR
   with a token that can read packages, then
   `bash deploy/deploy.sh ghcr.io/eyedema/ttstats-web:<sha> <sha>`. It
   migrates (a no-op for the same release), swaps, and smoke-tests through
   Pangolin. Check `curl -s https://pingpong.ubaldopuocci.org/healthz`.
   Redis starts empty, so no cache needs bumping.
9. **Cron and backups.** `crontab deploy/cron/ttstats.cron`; redo the VPS part
   of `deploy/backup/README.md` (drill key, `rclone.conf`, `backup.env`), then
   `deploy/backup/backup.sh` once by hand. The old drill key is gone with the
   box: re-encrypt nothing, just add the new drill public key to
   `BACKUP_AGE_RECIPIENTS` (older backups stay readable with the main key).
10. **GitHub secrets.** `SSH_HOST` (new IP) and `SSH_PRIVATE_KEY` if the key
    changed, so the next deploy reaches the new box.

## 2. Roll back a deploy by hand

A failed deploy rolls itself back (KAN-116). To go back on purpose:

```bash
bash deploy/deploy.sh ghcr.io/eyedema/ttstats-web:<older-sha> <older-sha>
```

or re-run that older commit's "Deploy to VPS" job in GitHub Actions. Both
swap the image and smoke-test it. **Migrations are not reverted**: migrations
are expand/contract, so the older release runs on the newer schema. If a
migration itself destroyed data, restore (section 4) instead.

## 3. Restore one table (a bad command, a bad job)

Restore the backup into a scratch Postgres, then copy the rows back:

```bash
# the newest backup taken BEFORE the damage; decrypt it as in 1.6, copy it here
docker run -d --name ttstats-scratch --network none --memory 512m \
  -e POSTGRES_PASSWORD=scratch postgres:16
sleep 5
docker exec -i ttstats-scratch pg_restore -U postgres -d postgres --no-owner --no-privileges < /root/restore.dump
# look before you copy
docker exec ttstats-scratch psql -U postgres -c 'select count(*) from pingpong_<table>'
# copy the table's rows out, into the prod db container, then in one transaction
docker exec ttstats-scratch psql -U postgres -c "\copy pingpong_<table> to stdout" > /root/rows.tsv
docker cp /root/rows.tsv "$(dcp ps -q db)":/tmp/rows.tsv
dcp exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
BEGIN;
-- decide first: delete the damaged rows, or only insert the missing ones
-- DELETE FROM pingpong_<table> WHERE ...;
\copy pingpong_<table> from '/tmp/rows.tsv'
COMMIT;
SQL
dcp exec -T db rm /tmp/rows.tsv
docker rm -f ttstats-scratch && shred -u /root/restore.dump /root/rows.tsv
```

Ratings are derived: after restoring matches or games, run
`dweb recalculate_ratings --dry-run`, then without `--dry-run`, then
`dweb cache_control --bump-leaderboard`.

## 4. Restore the whole database in place

Everything written since the backup is lost, including real matches.

```bash
IMAGE=$(docker inspect --format '{{.Config.Image}}' "$(dcp ps -q web)")   # BEFORE stopping
bash deploy/backup/backup.sh --tag before-restore      # keep what is there now
dcp stop web
dcp exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
dcp exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' < /root/restore.dump
WEB_IMAGE="$IMAGE" SENTRY_RELEASE="${IMAGE##*:}" dcp up -d --no-deps --wait web
shred -u /root/restore.dump
```

## Restarting web (an env change)

Never a bare `dcp up -d web`: it falls back to `:latest`. Pin the running
image, and `--no-deps` so `db` and `redis` (same `env_file`) are not recreated:

```bash
IMAGE=$(docker inspect --format '{{.Config.Image}}' "$(dcp ps -q web)")
WEB_IMAGE="$IMAGE" SENTRY_RELEASE="${IMAGE##*:}" dcp up -d --no-deps --wait web
```

## Take a backup by hand

Before anything risky (a manual migration, a data fix):

```bash
bash deploy/backup/backup.sh --tag before-<what>       # encrypted, off-box, in adhoc/
```

Until the backups are set up (KAN-117), a local plaintext dump instead:

```bash
dcp exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' \
  > ~/ttstats-$(date +%Y%m%d-%H%M).dump && ls -lh ~/ttstats-*.dump
```

## Legacy

`/root/scripts/backup-db.sh` (crontab `0 2 * * *`, log `/var/log/db-backup.log`)
predates this runbook. It points at `/var/www/ttstats`, which no longer
exists, so since the move to `/root/ttstats` it writes a 50-byte empty
`.sql.gz` to `/root/backups` every night and still prints "Backup
completed". Remove its crontab line once `deploy/backup/backup.sh` runs.

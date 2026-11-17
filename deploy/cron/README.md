# Host cron for scheduled jobs and backups

`ttstats.cron` runs `manage.py run_scheduled_jobs` inside the running `web`
container once a day at 03:15 UTC. The runner (`ttstats/pingpong/jobs.py`)
handles locking, run logging and failure isolation, so this is the only line
the host needs: a new job is registered in code, not here.

It also carries two host scripts that are not Django jobs, **commented out until
KAN-117 (remote + keys) is done** (see `deploy/backup/README.md`):

| When (UTC) | What | Log tag |
|---|---|---|
| 02:45 daily | `deploy/backup/backup.sh`: encrypted off-host `pg_dump` (KAN-118) | `ttstats-backup` |
| 04:30 on the 2nd | `deploy/backup/restore_drill.sh`: restore the newest backup into a throwaway container and check it (KAN-108) | `ttstats-drill` |

The backup runs before the 03:15 jobs so it captures the day before point
retention prunes anything. Each script reports to its own Sentry cron monitor;
the exit status is lost in the `| logger` pipe, so Sentry (not cron mail) is
where a failure shows. To enable: uncomment both lines and re-install below.

Jobs are **never** run from the deploy workflow. The SSH deploy step has a
timeout that has already failed deploys, and a job must not extend it.

## Install (once, and again after editing the file)

1. The `cd` path is the production checkout, `/root/ttstats` (the `WORK_DIR`
   secret). If the checkout moves, change it here and re-install.
2. As the user that runs `docker compose` for the deploy, install it as that
   user's crontab:

   ```sh
   crontab -l > ~/crontab.bak 2>/dev/null   # keep any existing entries
   crontab deploy/cron/ttstats.cron          # replaces the whole crontab
   crontab -l                                # check
   ```

   `crontab FILE` replaces the user's crontab; if `crontab.bak` had other
   entries, merge them into one file first.

## Verify

```sh
docker compose -f compose.prod.yml exec -T web python manage.py run_scheduled_jobs --list
docker compose -f compose.prod.yml exec -T web python manage.py run_scheduled_jobs --dry-run
journalctl -t ttstats-jobs --since today     # or: grep ttstats-jobs /var/log/syslog
journalctl -t ttstats-backup --since today   # once enabled
journalctl -t ttstats-drill --since "-31 days"
```

The admin also shows the run log under **Job runs**: every non-dry run writes a
row (succeeded, failed, skipped or interrupted). After the first night, record
the observed run on KAN-37.

# Host cron for scheduled jobs

`ttstats.cron` runs `manage.py run_scheduled_jobs` inside the running `web`
container once a day at 03:15 UTC. The runner (`ttstats/pingpong/jobs.py`)
handles locking, run logging and failure isolation, so this is the only line
the host needs: a new job is registered in code, not here.

Jobs are **never** run from the deploy workflow. The SSH deploy step has a
timeout that has already failed deploys, and a job must not extend it.

## Install (once, and again after editing the file)

1. If the deploy checkout (the `WORK_DIR` secret) is not `/opt/ttstats`, edit
   the `cd` path in the host's copy. Keep the committed copy generic.
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
```

The admin also shows the run log under **Job runs**: every non-dry run writes a
row (succeeded, failed, skipped or interrupted). After the first night, record
the observed run on KAN-37.

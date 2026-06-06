# Monday.com Sync

Deploy the repo on the server at:

```bash
/home/ubuntu/scripts/script-timecamp-projects-sync
```

## Required Config

Create `/home/ubuntu/scripts/script-timecamp-projects-sync/.env`:

```bash
TIMECAMP_API_TOKEN=...
TIMECAMP_TASK_ID=123

MONDAY_API_TOKEN=...
MONDAY_BOARD_IDS=1234
MONDAY_MEANDATORY_TAGS=Client
```

Optional config:

```bash
# Run only selected sync steps. Default is all actions.
TIMECAMP_SYNC_ACTIONS=tasks,archive,tags,mandatory_tags,users

# Useful for updating tags/users without creating or archiving tasks.
TIMECAMP_SYNC_ACTIONS=tags,mandatory_tags,users

# Skip assigning task mandatory tags when more than N new tags would be added.
# Leave unset for no limit.
TIMECAMP_MAX_MANDATORY_TAGS_TO_ADD=1
```

Available `TIMECAMP_SYNC_ACTIONS`: `tasks`, `archive`, `tags`, `mandatory_tags`, `users`.

## Manual Run

```bash
cd /home/ubuntu/scripts/script-timecamp-projects-sync
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python fetch_mondaycom.py
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python sync_projects.py
```

Backfill missing mandatory tags on time entries:

```bash
cd /home/ubuntu/scripts/script-timecamp-projects-sync
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python helpers/assign_mandatory_tags_to_time_entries.py --from 2026-02-01 --to "$(date +%Y-%m-%d)"
```

## Crontab

Edit cron:

```bash
crontab -e
```

Example daily sync:

```cron
15 2 * * * cd /home/ubuntu/scripts/script-timecamp-projects-sync && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python fetch_mondaycom.py >> /home/ubuntu/crontab_scripts/logs/mondaycom_fetch.log 2>&1 && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python sync_projects.py >> /home/ubuntu/crontab_scripts/logs/mondaycom_sync.log 2>&1
```

Example daily time-entry mandatory-tag backfill for the last 10 days:

```cron
45 2 * * * cd /home/ubuntu/scripts/script-timecamp-projects-sync && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python helpers/assign_mandatory_tags_to_time_entries.py --from $(date -d "10 days ago" +\%Y-\%m-\%d) --to $(date +\%Y-\%m-\%d) >> /home/ubuntu/crontab_scripts/logs/mondaycom_time_entry_tags.log 2>&1
```

Create the log directory if needed:

```bash
mkdir -p /home/ubuntu/crontab_scripts/logs
```

# Monday.com Sync

Deploy the repo on the server at:

```bash
/path/to/script-timecamp-projects-sync
```

## Required Config

Create `/path/to/script-timecamp-projects-sync/.env`:

```bash
TIMECAMP_API_TOKEN=...
TIMECAMP_TASK_ID=123

MONDAY_API_TOKEN=...
MONDAY_BOARD_IDS=1234
MONDAY_MEANDATORY_TAGS=Client
```

Optional config:

```bash
# Status column (title or id) that marks Monday items as done. Done items are
# not exported. The first column found on each board is used. Without it, the
# script uses the column with id "status", then the first status column with
# done labels. Check the "Done status column for ..." lines in the fetch log.
MONDAY_DONE_STATUS_COLUMNS=Item Status,Phase

# Subitem tags. A subitem normally inherits the mandatory tag of its parent row.
# When that tag is missing or a placeholder (a row that groups work for many
# clients), the subitem uses its own column instead. Each subitem column value
# is mapped to the tag value it has most often under rows with a real tag, for
# example "Brand X" -> "Client Y". Without such rows the column value is used.
MONDAY_SUBITEM_MEANDATORY_TAGS=Client=Brand
MONDAY_PLACEHOLDER_TAGS=All clients

# Run only selected sync steps. Default is all actions.
TIMECAMP_SYNC_ACTIONS=tasks,names,estimates,archive,tags,mandatory_tags,users

# Useful for updating tags/users without creating or archiving tasks.
TIMECAMP_SYNC_ACTIONS=tags,mandatory_tags,users

# Skip assigning task mandatory tags when more than N new tags would be added.
# Leave unset for no limit.
TIMECAMP_MAX_MANDATORY_TAGS_TO_ADD=1

# Cache verified task mandatory-tag assignments. Cold runs use the internal
# project-list tag payload in bulk when available, then write this cache.
# Delete this file to force a full mandatory-tag recheck.
TIMECAMP_MANDATORY_TAG_CACHE_FILE=data/timecamp_mandatory_tag_cache.json

# Remove direct TimeCamp task user assignments that are missing from assigned_users
# in the source JSON. Equivalent CLI flag: sync_projects.py --strict-user-sync.
TIMECAMP_STRICT_USER_SYNC=true
```

Available `TIMECAMP_SYNC_ACTIONS`: `tasks`, `names`, `estimates`, `archive`,
`tags`, `mandatory_tags`, `users`, `custom_fields`.

## Manual Run

```bash
cd /path/to/script-timecamp-projects-sync
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python fetch_mondaycom.py
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python sync_projects.py
```

`fetch_mondaycom.py --output-with-done tasks_with_done.json` also writes the
tasks of done items from the same fetch. `tasks.json` stays without them, so
`sync_projects.py` still archives finished work in TimeCamp. Use
`tasks_with_done.json` for time entry tags and the Monday export: finished work
keeps its time entries.

Backfill missing mandatory tags on time entries:

```bash
cd /path/to/script-timecamp-projects-sync
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python helpers/assign_mandatory_tags_to_time_entries.py --tasks-file tasks_with_done.json --from 2026-02-01 --to "$(date +%Y-%m-%d)"
```

By default the backfill only adds a tag when the entry has no tag from that
mandatory tag list. Add `--replace-mismatched` to also replace a tag from the
list that differs from the tag the task needs now, for example after the tag
source in Monday changed. Run it with `--dry-run` first: the summary lists
each planned "removed -> added" change with the number of entries and hours.
The new tag is added first and the old tag is removed only when TimeCamp
confirms the new one, so an entry never stays without a tag. Locked entries
are skipped and counted.

Add `--replaceable-tags "All clients"` to replace only the listed tags (for
example the placeholder values). An entry that also has another mismatched tag,
such as a tag that a person chose, is left as it is. Use this for scheduled
runs, so they never remove a tag that a person chose.

Export TimeCamp totals to a Monday numbers column on subitems (dry-run without
`--apply`). The export includes time on archived TimeCamp tasks:

```bash
cd /path/to/script-timecamp-projects-sync
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python export_monday_time_logged.py --tasks-file tasks_with_done.json --from 2026-01-01 --to "$(date +%Y-%m-%d)" --column-title "Time Tracked"
```

## Crontab

Edit cron:

```bash
crontab -e
```

Example daily sync:

```cron
15 2 * * * cd /path/to/script-timecamp-projects-sync && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python fetch_mondaycom.py >> /path/to/logs/mondaycom_fetch.log 2>&1 && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python sync_projects.py >> /path/to/logs/mondaycom_sync.log 2>&1
```

Write `tasks_with_done.json` in the same fetch:

```cron
15 * * * * cd /path/to/script-timecamp-projects-sync && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python fetch_mondaycom.py --output-with-done tasks_with_done.json >> /path/to/logs/mondaycom_fetch.log 2>&1
```

Example daily time-entry mandatory-tag backfill for the last 10 days:

```cron
45 2 * * * cd /path/to/script-timecamp-projects-sync && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python helpers/assign_mandatory_tags_to_time_entries.py --tasks-file tasks_with_done.json --replace-mismatched --replaceable-tags "All clients" --from $(date -d "10 days ago" +\%Y-\%m-\%d) --to $(date +\%Y-\%m-\%d) >> /path/to/logs/mondaycom_time_entry_tags.log 2>&1
```

Example daily export of TimeCamp totals to Monday:

```cron
0 7 * * * cd /path/to/script-timecamp-projects-sync && uv run --env-file .env --python 3.13 --with-requirements requirements.txt python export_monday_time_logged.py --tasks-file tasks_with_done.json --from 2026-01-01 --to $(date +\%Y-\%m-\%d) --column-title "Time Tracked" --apply >> /path/to/logs/mondaycom_time_export.log 2>&1
```

Create the log directory if needed:

```bash
mkdir -p /path/to/logs
```

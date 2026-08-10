# Multijira

## Setup

Install dependencies:
```bash
pip install -r requirements.txt
```

## Steps

1. Configure `.env` file with JIRA instances:
```
JIRA_INSTANCES='[{"name": "Jira Instance 1", "url": "https://your-domain.atlassian.net", "email": "your-email@example.com", "token": "your-api-token"}, {"name": "Jira Instance 2", "url": "https://another-domain.atlassian.net", "email": "your-email@example.com", "token": "another-api-token"}]'

# Optional: prefix issue names with their Jira key, e.g. "[TCD-123] Task name"
JIRA_PREFIX_ISSUE_KEY_TO_TASK_NAME=true
```

2. `python fetch_jira.py` and by default output to `tasks.json`
    - Check what we have in native Jira integration to try to match task_id pattern

```json
[
  {
    "name": "Jira Instance 1",
    "task_id": "org_913310",
    "parent_id": 0
  },
  {
    "name": "Jira Project 1",
    "task_id": "org_913310_proj_TCD",
    "parent_id": "org_913310"
  },
  {
    "name": "[TCD-12] Bug 1",
    "task_id": "org_913310_proj_TCD_TCD-12",
    "parent_id": "org_913310_proj_TCD",
    "original_estimate": "2h",
    "original_estimate_seconds": 7200
  },
  {
    "name": "[TCD-13] Epic 1",
    "task_id": "org_913310_proj_TCD_TCD-13",
    "parent_id": "org_913310_proj_TCD",
    "original_estimate": null,
    "original_estimate_seconds": null
  },
  {
    "name": "[TCD-14] Task 1",
    "task_id": "org_913310_proj_TCD_TCD-14",
    "parent_id": "org_913310_proj_TCD_TCD-13",
    "original_estimate": "1h 30m",
    "original_estimate_seconds": 5400
  },
  {
    "name": "[TCD-15] SubTask 1",
    "task_id": "org_913310_proj_TCD_TCD-15",
    "parent_id": "org_913310_proj_TCD_TCD-14",
    "original_estimate": "30m",
    "original_estimate_seconds": 1800
  },
  {
    "name": "Jira Instance 2",
    "task_id": "org_913311",
    "parent_id": 0
  },
  {
    "name": "Jira Project 1",
    "task_id": "org_913311_proj_TCD",
    "parent_id": "org_913311"
  }
]
```

Jira issue rows include the original estimate in Jira's readable format and in
seconds. Issues without an estimate contain `null`. The estimate is requested as
part of the existing bulk issue search, so it does not add per-issue API calls.

3. `python sync_projects.py` (by default looks for `tasks.json`). Jira estimates
   are synchronized to TimeCamp task hour budgets through the v3 billing-settings
   endpoint. Tasks without a Jira estimate are left unchanged. Existing TimeCamp
   names are updated only when they differ from the fetched Jira names.

## Export TimeCamp entries to Jira worklogs

The exporter mirrors time from TimeCamp tasks created by `fetch_jira.py` back to
the corresponding issue and Jira instance. It writes by default and runs on the
destination-neutral lifecycle documented in
[`time-entry-sync.md`](time-entry-sync.md).

The first run must specify the TimeCamp entry-date range to backfill:

```bash
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py --from 2026-08-01 --to 2026-08-10
```

After the first successful run, omit the dates. The exporter uses TimeCamp's
entry modification filter and deletion feed, so an entry from months ago is
still updated or deleted when somebody changes it today:

```bash
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py
```

Preview without changing Jira or the state file:

```bash
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py --dry-run
```

Restrict a backfill to one TimeCamp user by exact email:

```bash
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py --from 2026-08-09 --to 2026-08-10 --user-email person@example.com --dry-run
```

Unless `--state-file` is supplied, a filtered export uses its own per-user state
file. Supplying `--from` and `--to` again performs a manual backfill without
moving an existing incremental cursor. Use `--state-file PATH` or
`JIRA_EXPORT_STATE_FILE` to override `data/jira_time_entries_state.json`. Use
`--env-file PATH` to load another dotenv file explicitly.

### Mirroring rules

- A new TimeCamp entry creates a Jira worklog. Entries shorter than 60 seconds
  are not created.
- Changes to duration, start time, date, or note update the existing worklog.
- Moving an entry to another Jira issue or configured instance deletes the old
  worklog and creates it on the new issue.
- Deleting an entry, shortening it below 60 seconds, or moving it to a non-Jira
  task deletes its previously exported Jira worklog.
- Jira remaining estimates are never adjusted (`adjustEstimate=leave`).
- The API-token owner remains the worklog author. The comment starts with
  `TimeCamp user: Display Name <email>` and preserves the TimeCamp note below.

The version-2 state stores its `jira` adapter identifier, incremental cursor,
and TimeCamp-entry-to-Jira-worklog mappings. Existing version-1 Jira state is
migrated on the next live save. Keep it on persistent storage. Each worklog also
receives a hidden `timecamp.entry` property, allowing the next run to recover a
create that reached Jira before its local mapping was saved.

The cursor advances only when every entry succeeds. Mappings are saved after
each successful remote mutation, so retrying the inclusive modification window
is safe. Manual date-range backfills verify the remote worklog and detect manual
Jira edits or deletions. Any per-entry failure produces a non-zero exit code.

### Required permissions

For every configured instance, the Jira API-token user needs:

- Browse Projects
- Work on Issues
- Edit Own Worklogs
- Delete Own Worklogs

The TimeCamp token must read the intended users, user details, tasks, entries,
and deleted-entry data. Missing user identity or timezone data is an error.

## Someday

- If there will be a need for S3 aim for `python fetch_jira.py | python upload_s3.py --folder jira/tasks.json`

# Sync other systems into TimeCamp

This repository contains scripts to automate projects and tasks synchronization between TimeCamp and other systems:

- Synchronizing clients, projects and tasks into TimeCamp
- Exporting time entries from TimeCamp
- Synchronizing meandatory tags and assigned users

## Installation

1. Clone this repository:

   ```bash
   git clone https://github.com/timecamp-org/script-timecamp-projects-sync.git
   cd script-timecamp-projects-sync
   ```

2. Install the required dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Set up the environment variables:
   - Copy `.env.example` to `.env`
   - Fill in required environment variables and TimeCamp API credentials and other configuration in `.env`

## Usage

To set up automatic daily synchronization, you can use a task scheduler like cron (Linux/macOS) or Task Scheduler (Windows).

### `tasks.json` format

`tasks.json` is a JSON array of source tasks. This example contains every key
consumed by `sync_projects.py`:

```json
[
  {
    "name": "Acme",
    "task_id": "client_acme",
    "parent_id": 0
  },
  {
    "name": "Build reporting dashboard",
    "task_id": "project_42",
    "parent_id": "client_acme",
    "original_estimate_seconds": 7200,
    "mandatory_tags": {
      "Client": ["Acme"],
      "Work type": ["Development", "Reporting"]
    },
    "assigned_users": {
      "source_user_id_123": {
        "email": "developer@example.com",
        "username": "Developer Name"
      }
    }
  }
]
```

### Harvest ↔ TimeCamp Synchronization

```bash
python3 fetch_harvest.py
python3 sync_projects.py
python3 export_time_entries_harvest.py 2026-03-19 2026-03-19
```

### Toggl JSON → TimeCamp Synchronization

```bash
python3 fetch_toggl_json.py projects.json
python3 sync_projects.py
```

### Redmine ↔ TimeCamp Synchronization

```bash
python3 fetch_redmine_and_sync.py
python3 export_time_entries_redmine.py 2026-03-19 2026-03-19
```

### Multiple Azure DevOps instances ↔ TimeCamp Synchronization

```bash
python3 fetch_azuredevops.py
python3 sync_projects.py
```

### Multiple Jira instances ↔ TimeCamp Synchronization

```bash
python3 fetch_jira.py
python3 sync_projects.py

# First TimeCamp → Jira worklog backfill (writes by default)
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py --from 2026-08-01 --to 2026-08-10

# Later runs export entries changed or deleted since the last successful run
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py

# Preview without changing Jira or exporter state
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py --dry-run

# Preview a two-day export for exactly one TimeCamp user
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py --from 2026-08-09 --to 2026-08-10 --user-email person@example.com --dry-run

# Export every user configured in JIRA_USER_API_TOKENS
uv run --env-file .env --with-requirements requirements.txt python export_time_entries_jira.py --all-users
```

Set `JIRA_USER_API_TOKENS` to JSON keyed by user email and Jira base URL, such as
`{"person@example.com":{"https://one.atlassian.net":"token-one","https://two.atlassian.net":"token-two"}}`,
to authenticate a filtered `--user-email` export as that Jira user. `--all-users`
runs the same filtered export sequentially for every configured email and keeps
a separate state file for each user. It continues after individual failures and
returns a non-zero exit code if any user fails. A string value remains shorthand
for one token used on every instance. Missing instance mappings and exports with
neither `--user-email` nor `--all-users` fall back to the root credentials in
`JIRA_INSTANCES`. The selected token owner is the Jira worklog author. The
exporter records the original TimeCamp user's display name and email in the
comment. It updates, moves, or deletes Jira worklogs when the source changes and
skips Jira writes when the canonical payload is unchanged. See
[`docs/jira.md`](docs/jira.md) and
[`docs/time-entry-sync.md`](docs/time-entry-sync.md) for state, recovery,
permissions, and the reusable adapter contract.

### Zendesk → TimeCamp Synchronization

```bash
python3 fetch_zendesk.py
python3 sync_projects.py
```

### Datadog → TimeCamp Synchronization

Set `DATADOG_API_KEY` and `DATADOG_APP_KEY`. For a Datadog site outside US1,
also set `DATADOG_SITE` to its domain, such as `datadoghq.eu`.

```bash
python3 fetch_datadog.py --output tasks.json
python3 sync_projects.py --input tasks.json
```

The fetcher imports active Datadog Case Management cases under their projects
and active or stable incidents under their alphabetically first affected service.
Closed, resolved, completed, and archived records are omitted so the TimeCamp
sync can archive work that is no longer active.

See [`docs/datadog.md`](docs/datadog.md) for the hierarchy and short external ID
contract, required Datadog permissions, and local usage.

### TimeCamp → TimeCamp Synchronization

```bash
uv run --python 3.13 --with-requirements requirements.txt python fetch_timecamp.py --output task_tc.json
uv run --env-file .env --python 3.13 --with-requirements requirements.txt python sync_projects.py --input task_tc.json
```

### Monday.com → TimeCamp Synchronization

```bash
python3 fetch_mondaycom.py
python3 sync_projects.py

uv run --with-requirements requirements.txt python fetch_mondaycom.py
uv run --with-requirements requirements.txt python sync_projects.py

# Export TimeCamp totals back to Monday.com Time Tracked numbers columns.
# Dry-run by default. Main rows are not updated because Monday should tally subitems.
uv run --with-requirements requirements.txt python fetch_mondaycom.py
uv run --with-requirements requirements.txt python export_monday_time_logged.py --from 2026-06-01 --to 2026-06-18 --column-title "Time Tracked"
uv run --with-requirements requirements.txt python export_monday_time_logged.py --from 2026-06-01 --to 2026-06-18 --column-title "Time Tracked" --apply

# Only use this if main rows should be overwritten by the exporter too.
uv run --with-requirements requirements.txt python export_monday_time_logged.py --from 2026-06-01 --to 2026-06-18 --column-title "Time Tracked" --include-main-rows
```

### Limiting TimeCamp Sync Actions

By default, `sync_projects.py` runs all actions: creating missing tasks, updating changed
names and estimates, archiving stale tasks, creating/restoring mandatory tag lists and
tags, assigning mandatory tags to tasks, and assigning users to tasks.

Set `TIMECAMP_SYNC_ACTIONS` to a comma-separated list to run only selected actions:

```bash
# Only create/restore tags, assign mandatory tags, and assign users.
TIMECAMP_SYNC_ACTIONS=tags,mandatory_tags,users uv run --env-file .env --with-requirements requirements.txt python sync_projects.py
```

Available actions are `tasks`, `names`, `estimates`, `archive`, `tags`,
`mandatory_tags`, and `users`. The `names` action updates a TimeCamp task only
when its name differs from the source name. The `estimates` action copies non-null
`original_estimate_seconds` values to TimeCamp task hour budgets. Both actions
compare against data already returned by the bulk task request and only send
updates for changed values.

Run only task-name synchronization:

```bash
TIMECAMP_SYNC_ACTIONS=names uv run --env-file .env --with-requirements requirements.txt python sync_projects.py
```

Run only estimate synchronization:

```bash
TIMECAMP_SYNC_ACTIONS=estimates uv run --env-file .env --with-requirements requirements.txt python sync_projects.py
```

Mandatory tag assignment checks use a local cache at
`data/timecamp_mandatory_tag_cache.json` by default. The first run still checks
TimeCamp task tags to seed the cache, using TimeCamp's internal project-list tag
payload in bulk when available instead of reading tags one task at a time. Later
runs skip unchanged tasks when the TimeCamp task id, task `modify_time`, and
desired mandatory tag ids still match. Set `TIMECAMP_MANDATORY_TAG_CACHE_FILE`
to use a different cache path.

Enable strict user sync when TimeCamp task assignees should exactly match the
source JSON. This removes direct TimeCamp user assignments from synced tasks
when those users are missing from `assigned_users` in the JSON file:

```bash
TIMECAMP_STRICT_USER_SYNC=true TIMECAMP_SYNC_ACTIONS=users uv run --env-file .env --with-requirements requirements.txt python sync_projects.py
uv run --env-file .env --with-requirements requirements.txt python sync_projects.py --strict-user-sync
```

Strict mode only removes direct assignments on the task being synced. Inherited
assignments from parent tasks are left alone.

## Helpers

```bash
# Move all root level projects/tasks as a subtask
uv run --env-file .env --with-requirements requirements.txt python helpers/archive.py --subtask-of {task_id} --dry-run

# Assign random colors to root level tasks
python3 helpers/assign_random_apple_colors.py --dry-run
uv run --env-file .env --with-requirements requirements.txt python helpers/assign_random_apple_colors.py --dry-run

# Batch assign users to selected tasks
uv run --env-file .env --with-requirements requirements.txt python helpers/assign_users_to_task.py --task-ids 34523534,34523535 --user-ids 364263,364264

# Batch assign users to all root level tasks
uv run --env-file .env --with-requirements requirements.txt python helpers/assign_users_to_task.py --user-ids 364263,364264

# Fill missing mandatory tags from tasks.json on time entries for all account users
uv run --env-file .env --with-requirements requirements.txt python helpers/assign_mandatory_tags_to_time_entries.py --from 2026-06-01 --to 2026-06-05 --dry-run
uv run --env-file .env --with-requirements requirements.txt python helpers/assign_mandatory_tags_to_time_entries.py --from 2026-06-01 --to 2026-06-05
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT

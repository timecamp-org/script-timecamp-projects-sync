# Sync other systems into TimeCamp

This repository contains scripts to automate projects and tasks synchronization between TimeCamp and other systems:

- Synchronizing clients, projects and tasks into TimeCamp
- Exporting time entries from TimeCamp

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

### Harvest ↔ TimeCamp Synchronization

```bash
# Sync clients, projects
python3 fetch_harvest.py
python3 sync_projects.py

# Export time entries for a date range
python3 export_time_entries_harvest.py 2026-03-19 2026-03-19
```

### Toggl JSON → TimeCamp Synchronization

```bash
# Convert Toggl projects export into tasks.json, then sync it
python3 fetch_toggl_json.py projects.json
python3 sync_projects.py
```

### Redmine ↔ TimeCamp Synchronization

```bash
# Sync projects and tasks
python3 fetch_redmine_and_sync.py

# Export time entries for a date range
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
```

### Zendesk → TimeCamp Synchronization

```bash
# Convert Zendesk organizations and active tickets into tasks.json, then sync it
python3 fetch_zendesk.py
python3 sync_projects.py
```

### Monday.com → TimeCamp Synchronization

```bash
# Convert Monday boards, groups, items and subitems into tasks.json, then sync it
python3 fetch_mondaycom.py
python3 sync_projects.py
```

Monday task external IDs are written as `monday_*` to match TimeCamp's native Monday.com integration.
Set `MONDAY_MEANDATORY_TAGS=Client` to add item column values to JSON as
`"meandatory_tags": {"Client": ["client name"]}`. Multiple column titles can be comma-separated.
Monday people columns are exported on items/subitems as
`"assigned_users": {"external_user_id": {"email": "...", "username": "..."}}` when available.

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

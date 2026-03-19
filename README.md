# Sync other systems into TimeCamp

This repository contains scripts to automate projects and tasks synchronization between TimeCamp and other systems:

- Synchronizing clients, projects and tasks into TimeCamp
- Exporting time entries from TimeCamp

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/timecamp-org/script-timecamp-projects-sync.git
   cd script-timecamp-projects-sync
   ```

2. Install the required dependencies:
   ```
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

### Redmine ↔ TimeCamp Synchronization

```bash
# Sync projects and tasks
python3 fetch_redmine_and_sync.py

# Export time entries for a date range
python3 export_time_entries_redmine.py 2026-03-19 2026-03-19
```

### Azure DevOps ↔ TimeCamp Synchronization

```bash
python3 fetch_azuredevops.py
python3 sync_projects.py
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT

# script-timecamp-projects-sync

A collection of scripts for project management and data synchronization across multiple platforms.

## Description

This repository contains scripts to automate various project management tasks:
- Synchronizing projects between Redmine and TimeCamp
- Exporting time entries from TimeCamp to Redmine
- Fetching comprehensive project data from multiple Azure DevOps instances

## Features

### Redmine ↔ TimeCamp Sync
- Fetches projects from Redmine
- Updates existing projects in TimeCamp
- Creates new projects and tasks in TimeCamp if they don't exist
- Exports time entries from TimeCamp to Redmine for a specified date range

### Hierarchical Task Data Fetching & Sync
- Connects to multiple Azure DevOps organizations (configurable for other systems)
- Fetches all projects, epics, features, user stories, tasks, and bugs
- Organizes work items in hierarchical structure with dynamic level detection
- Excludes "Done" tasks from synchronization
- Exports comprehensive data to JSON format with unique IDs
- Universal sync to TimeCamp maintaining any parent-child hierarchy
- Archives TimeCamp tasks that are no longer active in source system

## Prerequisites

- Python 3.x
- Access to Redmine API (for Redmine sync)
- Access to TimeCamp API (for TimeCamp sync)
- Azure DevOps Personal Access Token(s) (for Azure DevOps data fetching)

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
   - Fill in your Redmine and TimeCamp API credentials and other configuration in `.env`

## Usage

### Redmine ↔ TimeCamp Synchronization

```bash
# Sync projects and tasks
python sync_projects.py

# Export time entries for a date range
python export_time_entries.py 2024-11-15 2024-11-20
```

To set up automatic daily synchronization, you can use a task scheduler like cron (Linux/macOS) or Task Scheduler (Windows).

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License.

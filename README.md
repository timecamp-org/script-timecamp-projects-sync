
# script-timecamp-projects-sync

A script to synchronize projects from Redmine to TimeCamp on a daily basis.

## Description

This script automates the process of synchronizing projects between Redmine and TimeCamp. It runs daily to ensure that the projects in TimeCamp are up-to-date with the projects in Redmine.

## Features

- Fetches projects from Redmine
- Updates existing projects in TimeCamp
- Creates new projects in TimeCamp if they don't exist
- Runs automatically on a daily schedule

## Prerequisites

- Python 3.x
- Access to Redmine API
- Access to TimeCamp API

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

To run the script manually:

```
python sync_projects.py
```

To set up automatic daily synchronization, you can use a task scheduler like cron (Linux/macOS) or Task Scheduler (Windows).

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License.
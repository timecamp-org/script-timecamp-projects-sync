
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
   git clone https://github.com/yourusername/script-timecamp-projects-sync.git
   cd script-timecamp-projects-sync
   ```

2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up the environment variables:
   - Copy `.env.example` to `.env`
   - Fill in your Redmine and TimeCamp API credentials in `.env`

## Usage

To run the script manually:

```
python sync_projects.py
```

To set up automatic daily synchronization, you can use a task scheduler like cron (Linux/macOS) or Task Scheduler (Windows).

## Configuration

Edit the `.env` file to customize the script's behavior:

```
REDMINE_URL=https://your-redmine-instance.com
REDMINE_API_KEY=your_redmine_api_key

TIMECAMP_API_TOKEN=your_timecamp_api_token

SYNC_INTERVAL=24
```

- `REDMINE_URL`: The URL of your Redmine instance
- `REDMINE_API_KEY`: Your Redmine API key
- `TIMECAMP_API_TOKEN`: Your TimeCamp API token
- `SYNC_INTERVAL`: Sync interval in hours (default is 24)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.


pagination
status
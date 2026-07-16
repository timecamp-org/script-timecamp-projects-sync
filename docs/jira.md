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

## Someday

- If there will be a need for S3 aim for `python fetch_jira.py | python upload_s3.py --folder jira/tasks.json`

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
    "name": "Bug 1",
    "task_id": "org_913310_proj_TCD_TCD-12",
    "parent_id": "org_913310_proj_TCD"
  },
  {
    "name": "Epic 1",
    "task_id": "org_913310_proj_TCD_TCD-13",
    "parent_id": "org_913310_proj_TCD"
  },
  {
    "name": "Task 1",
    "task_id": "org_913310_proj_TCD_TCD-14",
    "parent_id": "org_913310_proj_TCD_TCD-13"
  },
  {
    "name": "SubTask 1",
    "task_id": "org_913310_proj_TCD_TCD-15",
    "parent_id": "org_913310_proj_TCD_TCD-14"
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

2. `python sync_project_new.py` (by default looks for `tasks.json`)

## Someday

- If there will be a need for S3 aim for `python fetch_jira.py | python upload_s3.py --folder jira/tasks.json`


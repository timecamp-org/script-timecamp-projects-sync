# Datadog to TimeCamp synchronization

`fetch_datadog.py` reads active work from Datadog and writes the flat hierarchy
consumed by `sync_projects.py`.

## Imported hierarchy

| Datadog source | TimeCamp hierarchy | External ID |
| --- | --- | --- |
| Case Management | `Datadog Cases` | `dd_c` |
| Project | Child of `Datadog Cases` | `dd_c_p_<project UUID>` |
| Active case | Child of its project | `dd_c_<case UUID>` |
| Incidents | `Datadog Incidents` | `dd_i` |
| Affected service | Child of `Datadog Incidents` | `dd_i_s_<service hash>` |
| Active or stable incident | Child of its first alphabetical service | `dd_i_<incident UUID>` |

Cases are active when they are not archived and their status group is `SG_OPEN`
or `SG_IN_PROGRESS`. Incidents are active when they are not archived and their
state is `active` or `stable`. Missing projects and services use dedicated
`Unassigned project` and `Unassigned service` parents.

Closed, resolved, completed, and archived records are intentionally omitted.
When `sync_projects.py` runs with its default actions, their existing TimeCamp
tasks are archived.

## Credentials

Create a Datadog API key and an application key with `cases_read` and
`incident_read` permissions. Datadog documents those permissions in its
[Case Management API](https://docs.datadoghq.com/api/latest/case-management/get-all-projects/)
and [authorization scopes](https://docs.datadoghq.com/api/latest/scopes/).

Configure:

```dotenv
DD_API_KEY=your_datadog_api_key
DD_APP_KEY=your_datadog_application_key
DD_SITE=datadoghq.com
TIMECAMP_API_TOKEN=your_timecamp_api_token
```

`DD_SITE` defaults to `datadoghq.com`. Use the domain for your Datadog site,
for example `datadoghq.eu` or `us3.datadoghq.com`.

## Run locally

Fetch first so the JSON can be inspected before TimeCamp is changed:

```bash
uv run --env-file .env --python 3.13 --with-requirements requirements.txt \
  python fetch_datadog.py --output tasks.json
uv run --env-file .env --python 3.13 --with-requirements requirements.txt \
  python sync_projects.py --input tasks.json
```

If any Datadog request fails, the fetcher exits before replacing `tasks.json`.

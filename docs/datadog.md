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

## Run with Docker

The image runs one Datadog fetch followed by one TimeCamp sync; it does not run
a server.

```bash
docker build -t timecamp-projects-sync:datadog .
docker run --rm --env-file .env timecamp-projects-sync:datadog
```

The container runs as UID/GID `10001` and keeps generated JSON and the mandatory
tag cache in `/tmp`.

## Run as a Kubernetes CronJob

Build and push the image, then replace `registry.example.com` in
`kubernetes/datadog-sync-cronjob.yaml` with the real registry.

Create the secret without committing credentials:

```bash
kubectl create secret generic timecamp-datadog-sync \
  --from-literal=DD_API_KEY='replace-me' \
  --from-literal=DD_APP_KEY='replace-me' \
  --from-literal=TIMECAMP_API_TOKEN='replace-me'
kubectl apply -f kubernetes/datadog-sync-cronjob.yaml
```

The example runs daily at 02:00 in `Europe/Warsaw`, forbids overlapping jobs,
uses a read-only root filesystem, and stores transient files in an `emptyDir`.
Change the schedule and `DD_SITE` in the manifest for the target environment.

Trigger an immediate run from the CronJob template:

```bash
kubectl create job --from=cronjob/timecamp-datadog-sync \
  timecamp-datadog-sync-manual
```

# NetSuite ↔ TimeCamp POC

## Scope

The integration implements this flow:

1. SuiteQL reads active NetSuite projects and project tasks.
2. `fetch_netsuite.py` writes the common `tasks.json` contract.
3. `sync_projects.py` creates, renames, estimates, tags, and archives the matching
   TimeCamp hierarchy.
4. `export_time_entries_netsuite.py` maps TimeCamp entries back to their NetSuite
   employee, project, project task/activity, and CAPEX/OPEX classification.
5. The exporter upserts `timebill` records by `timecamp-{entry_id}` external ID.
   Re-running a date range updates the same records instead of duplicating them.

User provisioning and project-user assignment are not implemented. The exporter
only reads NetSuite employees and TimeCamp users to resolve the employee required
by a `timebill`. Email is the default key. Ambiguous or missing matches block an
applied export; use `time_export.employee_mapping` for explicit TimeCamp user ID
to NetSuite employee ID overrides.

## Authentication

Use OAuth 2.0 Client Credentials (M2M). NetSuite requires an integration record,
the `REST Web Services` scope, a role with the necessary record permissions, and
a certificate mapping. Configure:

```dotenv
NETSUITE_ACCOUNT_ID=1234567_SB1
NETSUITE_CLIENT_ID=...
NETSUITE_CERTIFICATE_ID=...
NETSUITE_PRIVATE_KEY_FILE=/absolute/path/to/private-key.pem
```

`NETSUITE_ACCESS_TOKEN` accepts a short-lived bearer token for local diagnosis,
but it is not a scheduler credential. Token-based OAuth 1.0 authentication is
deliberately not added: Oracle says that from NetSuite 2027.1 new TBA integrations
for REST web services cannot be created.

## Account discovery before the POC

NetSuite's REST schema is account-specific. Standard and custom fields must be
confirmed against the WCG Records Catalog; guessing them is unsafe. The client
supports the metadata endpoint, and the relevant record types are `job`,
`projecttask`, `employee`, and `timebill`.

Copy `netsuite_config.example.json` to `netsuite_config.json`. Both SuiteQL
queries must use a deterministic `ORDER BY`, because the REST endpoint is paged.
The importer expects these aliases:

| Query | Required aliases | Optional aliases |
| --- | --- | --- |
| `projects` | `id`, `name` | `parent_id`, `capex_opex`, `activity_id`, `is_inactive` |
| `project_tasks` | `id`, `name`, `project_id` | `parent_id`, `capex_opex`, `activity_id`, `estimated_work_hours`, `original_estimate_seconds`, `is_inactive` |
| `employees_query` | `id`, `email` | none |

The example uses standard field candidates, not a claim about WCG's schema.
Replace them when the Records Catalog or a real SuiteQL call proves otherwise.

## CAPEX/OPEX

Alias the WCG project/task classification field to `capex_opex` in SuiteQL. The
importer normalizes it through `classification.value_map`, inherits a missing task
classification from its parent/project, and assigns it under a mandatory TimeCamp
tag list. Unknown non-empty values stop the import instead of corrupting financial
classification. The example sets `classification.required` to `true`, so its
standard query skeleton deliberately cannot be used for synchronization until the
WCG classification field is added to the query.

For export, make an explicit choice in `time_export.classification`:

- `mode: "field"` writes the mapped value to a WCG `timebill` field.
- `mode: "project"` writes no classification field because the selected NetSuite
  project is the authoritative classification.
- `mode: "omit"` deliberately drops it. This is appropriate only if WCG confirms
  CAPEX/OPEX is irrelevant on individual time records.

The placeholders in the example config intentionally make `field` mode fail until
the real WCG field and list value IDs are entered.

## Activity and project task mapping

Time entered on an imported project task exports both the NetSuite project and
project-task IDs. If WCG uses a service item or another activity reference, alias
its ID as `activity_id` and set `time_export.fields.activity` to the corresponding
`timebill` field. `default_activity_id` is the fallback.

NetSuite custom forms can require extra fields such as approval status, subsidiary,
department, or location. Add invariant values to `time_export.fixed_fields`. Do not
apply an export until the dry-run payload passes against the sandbox metadata and
WCG's approval workflow.

## Commands

```bash
uv run --env-file .env --with-requirements requirements.txt \
  python fetch_netsuite.py --config netsuite_config.json --output tasks.json

TIMECAMP_SYNC_EXTERNAL_ID_PREFIX=netsuite_ \
  TIMECAMP_SYNC_ACTIONS=tasks,names,estimates,tags,mandatory_tags,archive \
  uv run --env-file .env --with-requirements requirements.txt \
  python sync_projects.py --input tasks.json

uv run --env-file .env --with-requirements requirements.txt \
  python export_time_entries_netsuite.py \
  --config netsuite_config.json --tasks tasks.json \
  --from 2026-08-01 --to 2026-08-03
```

The exporter is dry-run by default. `--apply` is all-or-nothing for local mapping
validation: any unmapped employee, invalid duration, missing project, or unresolved
CAPEX/OPEX value stops the run before the first NetSuite write. Network failure can
still interrupt a batch, but external-ID upserts make the same command safe to retry.

NetSuite stores `timebill.hours` at minute precision. `duration_rounding` supports
`nearest` (default), `floor`, `ceil`, or `reject`; use `reject` if WCG requires zero
rounding loss.

## Unresolved WCG decisions

- Which standard/custom records are the authoritative project and activity sources?
- What field represents project hierarchy, status, project manager, and CAPEX/OPEX?
- Is CAPEX/OPEX derived from the project or stored on each `timebill`?
- Is `caseTaskEvent` the correct project-task field in WCG's REST metadata?
- Is a service `item`, approval status, subsidiary, department, location, or memo required?
- Should an edited/deleted TimeCamp entry update/delete the NetSuite record, and what
  happens after approval or posting closes the accounting period?
- What date window, timezone cutoff, and approval state are eligible for export?

These are specification decisions, not implementation details. The POC should prove
them against a NetSuite sandbox before production credentials or scheduled writes are
allowed.

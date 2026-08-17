# Universal time-entry synchronization

`src/time_entry_sync.py` implements a reusable one-TimeCamp-entry-to-one-remote-
entry lifecycle. Jira is the first adapter. Future destinations should implement
the adapter protocol instead of copying orchestration, cursor, or state logic.
Monday.com remains separate because it exports aggregate task totals.

## Lifecycle

The first run requires an entry-date range. A successful first run stores today's
date as the modification cursor. Later runs omit dates and request both TimeCamp
entries modified since the inclusive cursor and the deletion feed for the same
window. A deletion wins if the same entry appears in both responses.

For each entry, the engine can:

- recover a missing local mapping from a destination source marker;
- create, update, move, or delete a destination entry;
- recreate a mapped remote entry that returns `404`;
- remove entries deleted in TimeCamp or moved off the integration;
- skip destination reads and writes when a saved fingerprint is unchanged.

Manual date-range runs read and compare mapped remote entries, detecting manual
edits and missing entries. Incremental runs trust matching saved fingerprints.

Mappings are saved immediately after every successful remote mutation. The
cursor advances only after a live run with no failures. Dry-run may read and
recover remote data, but never writes remote data or state. Missing deletes are
successful; permission and locking failures are reported and hold the cursor.

## State

Each destination uses a separate version-2 JSON file:

```json
{
  "version": 2,
  "adapter": "jira",
  "cursor_date": "2026-08-10",
  "entries": {
    "123": {
      "target_key": "org_1|TCD-123",
      "remote_id": "789",
      "source_fingerprint": "v1:..."
    }
  }
}
```

Writes use a temporary file, `fsync`, and atomic replacement. State from another
adapter is rejected. Existing Jira version-1 state is decoded and written as
version 2 on the next live save. Keep state on persistent, backed-up storage.

## Common CLI

The shared CLI helpers provide `--from`, `--to`, `--dry-run`, `--state-file`,
`--user-email`, and `--env-file`. A future wrapper may additionally preserve
legacy positional dates.

## Adding an adapter

Implement `TimeEntryTargetAdapter` and keep destination-specific API shapes out
of the engine. An adapter must:

- prepare create/update payloads and a canonical fingerprint;
- define its immutable `target_key` boundary;
- recover, read, create, update, and delete one remote entry;
- recognize only its real not-found exception as missing;
- fail duplicate markers and forbidden mutations.

Use `TimeEntrySyncState` with a unique adapter ID and wrap the engine in a thin
CLI. Reuse `tests/test_time_entry_sync.py` for lifecycle behavior, then add
destination tests for payload normalization, marker recovery, permissions, and
CLI compatibility.

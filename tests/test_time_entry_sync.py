import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.time_entry_sync import (
    PreparedRemoteEntry,
    RecoveredRemoteEntry,
    SyncRunContext,
    TimeEntrySyncEngine,
    canonical_fingerprint,
)
from src.time_entry_sync_state import SyncMapping, TimeEntrySyncState


TODAY = date(2026, 8, 10)


def source_entry(entry_id, *, value="new", target="project:1", user_id=7):
    return {
        "id": str(entry_id),
        "user_id": str(user_id),
        "task_id": "10",
        "date": "2026-01-03",
        "start_time": "09:00:00",
        "duration": "3600",
        "description": value,
        "addons_external_id": target,
    }


class FakeTimeCampClient:
    def __init__(self, entries=None, deletions=None):
        self.entries = entries or []
        self.deletions = deletions or []
        self.entry_calls = []

    def get_tasks(self):
        return []

    def get_time_entries(self, *args, **kwargs):
        self.entry_calls.append((args, kwargs))
        return list(self.entries)

    def get_time_entry_deletions(self, *args, **kwargs):
        return list(self.deletions)

    def get_user_details(self, user_ids):
        return [
            {
                "user_id": str(user_id),
                "email": f"user{user_id}@example.com",
                "display_name": f"User {user_id}",
            }
            for user_id in user_ids
        ]


class MissingRemoteError(Exception):
    pass


class FakeAdapter:
    adapter_id = "fake"
    target_name = "Fake"
    minimum_duration_seconds = 1

    def __init__(self):
        self.created = []
        self.updated = []
        self.deleted = []
        self.reads = []
        self.recoveries = {}
        self.remote = {}
        self.fail_update = False
        self.delete_missing = False
        self.next_id = 100

    def prepare_run(self, context: SyncRunContext):
        self.context = context

    def recovery_target(self, entry, task):
        return entry.external_task_id

    def prepare_entry(self, entry, user, task):
        if not entry.external_task_id or entry.external_task_id == "off":
            return None
        payload = {"value": str(entry.description)}
        return PreparedRemoteEntry(
            target_key=entry.external_task_id,
            create_payload=payload,
            update_payload=payload,
            source_fingerprint=canonical_fingerprint(payload),
            target_label=entry.external_task_id,
        )

    def ineligible_reason(self, entry, task):
        return "off integration"

    def recover(self, entry, target_key):
        value = self.recoveries.get(entry.entry_id)
        if isinstance(value, Exception):
            raise value
        return value

    def read(self, mapping):
        self.reads.append(mapping)
        key = (mapping.target_key, mapping.remote_id)
        if key not in self.remote:
            raise MissingRemoteError()
        return self.remote[key]

    def fingerprint_remote(self, payload):
        return canonical_fingerprint(payload)

    def create(self, prepared):
        self.next_id += 1
        remote_id = str(self.next_id)
        self.created.append((prepared.target_key, dict(prepared.create_payload)))
        self.remote[(prepared.target_key, remote_id)] = dict(prepared.create_payload)
        return remote_id

    def update(self, mapping, prepared):
        if self.fail_update:
            raise RuntimeError("locked")
        self.updated.append((mapping, dict(prepared.update_payload)))
        self.remote[(mapping.target_key, mapping.remote_id)] = dict(
            prepared.update_payload
        )

    def delete(self, mapping):
        if self.delete_missing:
            raise MissingRemoteError()
        self.deleted.append(mapping)
        self.remote.pop((mapping.target_key, mapping.remote_id), None)

    def is_missing(self, exc):
        return isinstance(exc, MissingRemoteError)

    def describe_mapping(self, mapping):
        return f"Fake {mapping.target_key}/{mapping.remote_id}"


class TimeEntrySyncEngineTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "state.json"

    def state(self, cursor=None, entries=None):
        state = TimeEntrySyncState(self.path, "fake", cursor, entries)
        if cursor or entries:
            state.save()
        return state

    def test_initial_create_persists_v2_mapping_and_cursor(self):
        state = self.state()
        adapter = FakeAdapter()
        result = TimeEntrySyncEngine(
            FakeTimeCampClient([source_entry(1)]),
            adapter,
            state,
            today=TODAY,
        ).run(date(2026, 1, 1), date(2026, 1, 3))

        self.assertEqual(result.created, 1)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["version"], 2)
        self.assertEqual(raw["adapter"], "fake")
        self.assertEqual(raw["cursor_date"], "2026-08-10")
        self.assertEqual(raw["entries"]["1"]["target_key"], "project:1")

    def test_incremental_updates_old_entry_and_deletion_wins(self):
        old = canonical_fingerprint({"value": "old"})
        state = self.state(
            "2026-08-01",
            {
                "1": SyncMapping("project:1", "11", old),
                "2": SyncMapping("project:1", "12", old),
            },
        )
        adapter = FakeAdapter()
        result = TimeEntrySyncEngine(
            FakeTimeCampClient(
                [source_entry(1), source_entry(2)],
                [{"entry_id": "2", "user_id": "7", "date": "2025-01-01"}],
            ),
            adapter,
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.deleted, 1)
        self.assertEqual([mapping.remote_id for mapping in adapter.deleted], ["12"])
        self.assertIsNone(state.get("2"))

    def test_incremental_fingerprint_skips_remote_read_and_write(self):
        fingerprint = canonical_fingerprint({"value": "new"})
        state = self.state(
            "2026-08-01",
            {"1": SyncMapping("project:1", "11", fingerprint)},
        )
        adapter = FakeAdapter()
        result = TimeEntrySyncEngine(
            FakeTimeCampClient([source_entry(1)]),
            adapter,
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.unchanged, 1)
        self.assertEqual(adapter.reads, [])
        self.assertEqual(adapter.updated, [])

    def test_manual_backfill_detects_remote_drift(self):
        fingerprint = canonical_fingerprint({"value": "new"})
        state = self.state(
            "2026-08-01",
            {"1": SyncMapping("project:1", "11", fingerprint)},
        )
        adapter = FakeAdapter()
        adapter.remote[("project:1", "11")] = {"value": "manual edit"}
        result = TimeEntrySyncEngine(
            FakeTimeCampClient([source_entry(1)]),
            adapter,
            state,
            today=TODAY,
        ).run(date(2026, 1, 1), date(2026, 1, 3))

        self.assertEqual(result.updated, 1)
        self.assertEqual(len(adapter.reads), 1)

    def test_missing_remote_is_recreated(self):
        state = self.state(
            "2026-08-01",
            {"1": SyncMapping("project:1", "missing")},
        )
        adapter = FakeAdapter()
        result = TimeEntrySyncEngine(
            FakeTimeCampClient([source_entry(1)]),
            adapter,
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.created, 1)
        self.assertNotEqual(state.get("1").remote_id, "missing")

    def test_target_change_deletes_then_creates_as_move(self):
        state = self.state(
            "2026-08-01",
            {"1": SyncMapping("project:old", "11")},
        )
        adapter = FakeAdapter()
        result = TimeEntrySyncEngine(
            FakeTimeCampClient([source_entry(1, target="project:new")]),
            adapter,
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.moved, 1)
        self.assertEqual(adapter.deleted[0].remote_id, "11")
        self.assertEqual(state.get("1").target_key, "project:new")

    def test_missing_remote_delete_is_successful(self):
        state = self.state(
            "2026-08-01",
            {"1": SyncMapping("project:1", "missing")},
        )
        adapter = FakeAdapter()
        adapter.delete_missing = True
        result = TimeEntrySyncEngine(
            FakeTimeCampClient(
                deletions=[{"entry_id": "1", "user_id": "7", "date": "2026-01-03"}]
            ),
            adapter,
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.deleted, 1)
        self.assertTrue(result.successful)
        self.assertIsNone(state.get("1"))

    def test_dry_run_recovery_never_writes_remote_or_state(self):
        state = self.state()
        adapter = FakeAdapter()
        adapter.recoveries["1"] = RecoveredRemoteEntry("11", "project:1")
        adapter.remote[("project:1", "11")] = {"value": "old"}
        result = TimeEntrySyncEngine(
            FakeTimeCampClient([source_entry(1)]),
            adapter,
            state,
            dry_run=True,
            today=TODAY,
        ).run(date(2026, 1, 1), date(2026, 1, 3))

        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.updated, 1)
        self.assertEqual(adapter.updated, [])
        self.assertFalse(self.path.exists())

    def test_failure_holds_cursor_and_returns_error(self):
        state = self.state(
            "2026-08-01",
            {"1": SyncMapping("project:1", "11", "old")},
        )
        adapter = FakeAdapter()
        adapter.fail_update = True
        result = TimeEntrySyncEngine(
            FakeTimeCampClient([source_entry(1)]),
            adapter,
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.failed, 1)
        self.assertEqual(
            TimeEntrySyncState.load(self.path, "fake").cursor_date,
            "2026-08-01",
        )


class TimeEntrySyncStateTest(unittest.TestCase):
    def test_rejects_state_for_another_adapter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            TimeEntrySyncState(path, "harvest").save()
            with self.assertRaisesRegex(ValueError, "belongs to adapter"):
                TimeEntrySyncState.load(path, "redmine")


if __name__ == "__main__":
    unittest.main()

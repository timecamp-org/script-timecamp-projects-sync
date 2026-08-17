import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import call, patch

import requests

from export_time_entries_jira import (
    JiraTimeEntryExporter,
    build_jira_clients,
    build_jira_worklog_payload,
    filtered_state_file,
    find_timecamp_user_id,
    parse_args,
    worklog_payload_fingerprint,
)
from src.jira_client import load_jira_user_api_tokens
from src.jira_export_state import JiraExportState, JiraWorklogMapping

TODAY = date(2026, 8, 10)


def jira_external_id(instance_id, project="TCD", number=123):
    return f"{instance_id}_proj_{project}_{project}-{number}"


def timecamp_entry(
    entry_id,
    *,
    external_task_id=None,
    task_id=10,
    duration=3600,
    user_id=7,
    description="Worked on it",
):
    return {
        "id": entry_id,
        "duration": str(duration),
        "user_id": str(user_id),
        "user_name": "Ada Lovelace",
        "task_id": str(task_id),
        "date": "2026-08-09",
        "start_time": "09:15:30",
        "description": description,
        "addons_external_id": external_task_id or "",
    }


def timecamp_user(user_id=7):
    return {
        "user_id": str(user_id),
        "display_name": "Ada Lovelace",
        "email": "ada@example.com",
        "time_zone": "0",
    }


class FakeTimeCampClient:
    def __init__(self, *, tasks=None, entries=None, deletions=None, users=None):
        self.tasks = tasks or []
        self.entries = entries or []
        self.deletions = deletions or []
        self.users = users or []
        self.entry_calls = []
        self.deletion_calls = []

    def get_tasks(self):
        return self.tasks

    def get_time_entries(self, *args, **kwargs):
        self.entry_calls.append((args, kwargs))
        return self.entries

    def get_time_entry_deletions(self, *args, **kwargs):
        self.deletion_calls.append((args, kwargs))
        return self.deletions

    def get_user_details(self, user_ids):
        return [user for user in self.users if int(user["user_id"]) in user_ids]


class FakeJiraClient:
    def __init__(
        self,
        *,
        remote_map=None,
        remote_worklogs=None,
        update_404=False,
        fail_create=False,
    ):
        self.remote_map = remote_map or {}
        self.remote_worklogs = remote_worklogs or {}
        self.update_404 = update_404
        self.fail_create = fail_create
        self.created = []
        self.updated = []
        self.deleted = []
        self.map_reads = []
        self.worklog_reads = []
        self.next_worklog_id = 9000

    def get_timecamp_worklog_map(self, issue_key):
        self.map_reads.append(issue_key)
        return dict(self.remote_map.get(issue_key, {}))

    def create_worklog(self, issue_key, payload, adjust_estimate="leave"):
        if self.fail_create:
            raise RuntimeError("Jira unavailable")
        self.next_worklog_id += 1
        worklog_id = str(self.next_worklog_id)
        self.created.append((issue_key, payload, adjust_estimate, worklog_id))
        return {"id": worklog_id}

    def get_worklog(self, issue_key, worklog_id):
        self.worklog_reads.append((issue_key, worklog_id))
        return self.remote_worklogs.get(
            (issue_key, worklog_id),
            {
                "started": "2000-01-01T00:00:00.000+0000",
                "timeSpentSeconds": 60,
                "comment": {
                    "type": "doc",
                    "version": 1,
                    "content": [],
                },
            },
        )

    def update_worklog(
        self,
        issue_key,
        worklog_id,
        payload,
        adjust_estimate="leave",
    ):
        if self.update_404:
            response = requests.Response()
            response.status_code = 404
            response.url = f"https://jira.test/{issue_key}/{worklog_id}"
            raise requests.HTTPError(response=response)
        self.updated.append((issue_key, worklog_id, payload, adjust_estimate))
        return {"id": worklog_id}

    def delete_worklog(
        self,
        issue_key,
        worklog_id,
        adjust_estimate="leave",
    ):
        self.deleted.append((issue_key, worklog_id, adjust_estimate))


class FailingSaveState(JiraExportState):
    def save(self):
        raise OSError("state storage unavailable")


class JiraPayloadTest(unittest.TestCase):
    def test_builds_timezone_identity_note_and_recovery_property(self):
        entry = timecamp_entry(101, description="First line\nSecond line")

        payload = build_jira_worklog_payload(
            entry,
            timecamp_user(),
            entry_id="101",
        )

        self.assertEqual(payload["started"], "2026-08-09T09:15:30.000+0200")
        self.assertEqual(payload["timeSpentSeconds"], 3600)
        self.assertEqual(
            payload["comment"]["content"],
            [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "TimeCamp user: Ada Lovelace <ada@example.com>",
                        }
                    ],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "First line"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Second line"}],
                },
            ],
        )
        self.assertEqual(
            payload["properties"],
            [{"key": "timecamp.entry", "value": {"entryId": "101"}}],
        )

    def test_started_offset_includes_timecamp_server_dst_and_user_adjustment(self):
        winter_entry = timecamp_entry(101)
        winter_entry["date"] = "2026-01-09"
        user = timecamp_user()
        user["time_zone"] = "-3600"

        payload = build_jira_worklog_payload(winter_entry, user)

        self.assertEqual(payload["started"], "2026-01-09T09:15:30.000+0000")

    def test_requires_user_identity_and_timezone(self):
        entry = timecamp_entry(101)

        with self.assertRaisesRegex(ValueError, "display name and email"):
            build_jira_worklog_payload(
                entry,
                {"user_id": 7, "display_name": "", "email": "", "time_zone": 0},
            )

        with self.assertRaisesRegex(ValueError, "valid time_zone"):
            build_jira_worklog_payload(
                entry,
                {"user_id": 7, "display_name": "Ada", "email": "a@b.test"},
            )

    def test_fingerprint_treats_equivalent_timezone_representations_as_equal(self):
        payload = build_jira_worklog_payload(timecamp_entry(101), timecamp_user())
        jira_payload = dict(payload)
        jira_payload["started"] = "2026-08-09T03:15:30.000-0400"

        self.assertEqual(
            worklog_payload_fingerprint(payload),
            worklog_payload_fingerprint(jira_payload),
        )


class JiraExporterTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "jira-state.json"

    def new_state(self, cursor=None, mappings=None):
        state = JiraExportState.load(self.state_path)
        state.cursor_date = cursor
        for entry_id, mapping in (mappings or {}).items():
            state.set(entry_id, mapping)
        if cursor is not None or mappings:
            state.save()
        return state

    def test_first_backfill_creates_worklog_and_initializes_cursor(self):
        entry = timecamp_entry(101, external_task_id=jira_external_id("org_1"))
        tc = FakeTimeCampClient(entries=[entry], users=[timecamp_user()])
        jira = FakeJiraClient()
        state = self.new_state()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run(date(2026, 8, 1), date(2026, 8, 9))

        self.assertTrue(result.successful)
        self.assertEqual(result.created, 1)
        self.assertEqual(tc.entry_calls, [((date(2026, 8, 1), date(2026, 8, 9)), {})])
        self.assertEqual(jira.created[0][0], "TCD-123")
        self.assertEqual(jira.created[0][2], "leave")
        persisted = JiraExportState.load(self.state_path)
        self.assertEqual(persisted.cursor_date, "2026-08-10")
        self.assertEqual(persisted.get("101").issue_key, "TCD-123")
        self.assertIsNotNone(persisted.get("101").source_fingerprint)

    def test_incremental_run_updates_and_propagates_deletion(self):
        mappings = {
            "101": JiraWorklogMapping("org_1", "TCD-123", "5001"),
            "102": JiraWorklogMapping("org_1", "TCD-124", "5002"),
        }
        state = self.new_state("2026-08-07", mappings)
        tc = FakeTimeCampClient(
            entries=[timecamp_entry(101, external_task_id=jira_external_id("org_1"))],
            deletions=[{"entry_id": "102", "task_id": "11"}],
            users=[timecamp_user()],
        )
        jira = FakeJiraClient()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.deleted, 1)
        self.assertEqual(
            tc.entry_calls,
            [((), {"modify_from": "2026-08-07", "modify_to": TODAY})],
        )
        self.assertEqual(
            tc.deletion_calls,
            [(("2026-08-07", TODAY), {})],
        )
        self.assertEqual(jira.updated[0][0:2], ("TCD-123", "5001"))
        self.assertEqual(jira.updated[0][3], "leave")
        self.assertEqual(jira.deleted, [("TCD-124", "5002", "leave")])
        persisted = JiraExportState.load(self.state_path)
        self.assertIsNone(persisted.get("102"))
        self.assertIsNotNone(persisted.get("101").source_fingerprint)
        self.assertEqual(persisted.cursor_date, "2026-08-10")

    def test_manual_backfill_skips_matching_remote_worklog_and_seeds_fingerprint(self):
        entry = timecamp_entry(101, external_task_id=jira_external_id("org_1"))
        payload = build_jira_worklog_payload(entry, timecamp_user())
        state = self.new_state(
            "2026-08-07",
            {"101": JiraWorklogMapping("org_1", "TCD-123", "5001")},
        )
        tc = FakeTimeCampClient(entries=[entry], users=[timecamp_user()])
        jira = FakeJiraClient(
            remote_worklogs={("TCD-123", "5001"): payload}
        )

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run(date(2026, 8, 9), date(2026, 8, 10))

        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(jira.updated, [])
        self.assertEqual(jira.worklog_reads, [("TCD-123", "5001")])
        mapping = JiraExportState.load(self.state_path).get("101")
        self.assertEqual(
            mapping.source_fingerprint,
            worklog_payload_fingerprint(payload),
        )

    def test_incremental_run_skips_matching_saved_fingerprint_without_jira_read(self):
        entry = timecamp_entry(101, external_task_id=jira_external_id("org_1"))
        fingerprint = worklog_payload_fingerprint(
            build_jira_worklog_payload(entry, timecamp_user())
        )
        state = self.new_state(
            "2026-08-07",
            {
                "101": JiraWorklogMapping(
                    "org_1",
                    "TCD-123",
                    "5001",
                    fingerprint,
                )
            },
        )
        tc = FakeTimeCampClient(entries=[entry], users=[timecamp_user()])
        jira = FakeJiraClient()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(jira.worklog_reads, [])
        self.assertEqual(jira.updated, [])

    def test_moves_worklog_across_jira_instances(self):
        state = self.new_state(
            "2026-08-07",
            {"101": JiraWorklogMapping("org_1", "OLD-1", "5001")},
        )
        tc = FakeTimeCampClient(
            entries=[
                timecamp_entry(
                    101,
                    external_task_id=jira_external_id("org_2", "NEW", 2),
                )
            ],
            users=[timecamp_user()],
        )
        old_jira = FakeJiraClient()
        new_jira = FakeJiraClient()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": old_jira, "org_2": new_jira},
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.moved, 1)
        self.assertEqual(old_jira.deleted, [("OLD-1", "5001", "leave")])
        self.assertEqual(new_jira.created[0][0], "NEW-2")
        mapping = JiraExportState.load(self.state_path).get("101")
        self.assertEqual(mapping.instance_id, "org_2")
        self.assertEqual(mapping.issue_key, "NEW-2")

    def test_deletes_existing_worklog_for_subminute_or_non_jira_entry(self):
        state = self.new_state(
            "2026-08-07",
            {
                "101": JiraWorklogMapping("org_1", "TCD-123", "5001"),
                "102": JiraWorklogMapping("org_1", "TCD-124", "5002"),
            },
        )
        tc = FakeTimeCampClient(
            entries=[
                timecamp_entry(
                    101,
                    external_task_id=jira_external_id("org_1"),
                    duration=59,
                ),
                timecamp_entry(102, external_task_id="monday_123"),
            ]
        )
        jira = FakeJiraClient()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.deleted, 2)
        self.assertEqual(
            jira.deleted,
            [
                ("TCD-123", "5001", "leave"),
                ("TCD-124", "5002", "leave"),
            ],
        )

    def test_skips_time_logged_on_jira_project_parent(self):
        state = self.new_state("2026-08-07")
        tc = FakeTimeCampClient(
            entries=[timecamp_entry(101, external_task_id="org_1_proj_TCD")]
        )
        jira = FakeJiraClient()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run()

        self.assertTrue(result.successful)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(jira.created, [])

    def test_user_filter_is_sent_to_timecamp_and_enforced_locally(self):
        state = self.new_state()
        tc = FakeTimeCampClient(
            entries=[
                timecamp_entry(101, external_task_id=jira_external_id("org_1")),
                timecamp_entry(
                    102,
                    external_task_id=jira_external_id("org_1", number=124),
                    user_id=8,
                ),
            ],
            users=[timecamp_user()],
        )
        jira = FakeJiraClient()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
            user_ids=[7],
        ).run(date(2026, 8, 9), date(2026, 8, 10))

        self.assertTrue(result.successful)
        self.assertEqual(result.created, 1)
        self.assertEqual(
            tc.entry_calls,
            [
                (
                    (date(2026, 8, 9), date(2026, 8, 10)),
                    {"user_ids": [7]},
                )
            ],
        )
        self.assertEqual([item[0] for item in jira.created], ["TCD-123"])

    def test_recovers_mapping_from_jira_property_before_updating(self):
        entry = timecamp_entry(101, external_task_id=jira_external_id("org_1"))
        tc = FakeTimeCampClient(entries=[entry], users=[timecamp_user()])
        jira = FakeJiraClient(remote_map={"TCD-123": {"101": "7001"}})
        state = self.new_state()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run(date(2026, 8, 1), date(2026, 8, 9))

        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.created, 0)
        self.assertEqual(jira.updated[0][0:2], ("TCD-123", "7001"))
        self.assertEqual(
            JiraExportState.load(self.state_path).get("101").worklog_id,
            "7001",
        )

    def test_recovers_deleted_entry_mapping_from_jira_property(self):
        state = self.new_state("2026-08-07")
        tc = FakeTimeCampClient(
            tasks=[
                {
                    "task_id": "10",
                    "external_task_id": jira_external_id("org_1"),
                }
            ],
            deletions=[{"entry_id": "101", "task_id": "10"}],
        )
        jira = FakeJiraClient(remote_map={"TCD-123": {"101": "7001"}})

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.deleted, 1)
        self.assertEqual(jira.deleted, [("TCD-123", "7001", "leave")])
        self.assertIsNone(JiraExportState.load(self.state_path).get("101"))

    def test_recreates_worklog_when_saved_jira_id_returns_404(self):
        state = self.new_state(
            "2026-08-07",
            {"101": JiraWorklogMapping("org_1", "TCD-123", "missing")},
        )
        tc = FakeTimeCampClient(
            entries=[timecamp_entry(101, external_task_id=jira_external_id("org_1"))],
            users=[timecamp_user()],
        )
        jira = FakeJiraClient(update_404=True)

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.created, 1)
        self.assertEqual(result.updated, 0)
        self.assertNotEqual(
            JiraExportState.load(self.state_path).get("101").worklog_id,
            "missing",
        )

    def test_dry_run_does_not_write_jira_or_state(self):
        tc = FakeTimeCampClient(
            entries=[timecamp_entry(101, external_task_id=jira_external_id("org_1"))],
            users=[timecamp_user()],
        )
        jira = FakeJiraClient()
        state = self.new_state()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            dry_run=True,
            today=TODAY,
        ).run(date(2026, 8, 1), date(2026, 8, 9))

        self.assertEqual(result.created, 1)
        self.assertEqual(jira.created, [])
        self.assertFalse(self.state_path.exists())
        self.assertIsNone(state.cursor_date)

    def test_partial_failure_does_not_advance_cursor(self):
        state = self.new_state("2026-08-07")
        tc = FakeTimeCampClient(
            entries=[timecamp_entry(101, external_task_id=jira_external_id("org_1"))],
            users=[timecamp_user()],
        )
        jira = FakeJiraClient(fail_create=True)

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run()

        self.assertEqual(result.failed, 1)
        self.assertEqual(
            JiraExportState.load(self.state_path).cursor_date,
            "2026-08-07",
        )

    def test_state_save_failure_stops_before_any_later_remote_write(self):
        state = FailingSaveState(self.state_path, cursor_date="2026-08-07")
        tc = FakeTimeCampClient(
            entries=[
                timecamp_entry(101, external_task_id=jira_external_id("org_1")),
                timecamp_entry(
                    102,
                    external_task_id=jira_external_id("org_1", number=124),
                ),
            ],
            users=[timecamp_user()],
        )
        jira = FakeJiraClient()

        with self.assertRaisesRegex(OSError, "state storage unavailable"):
            JiraTimeEntryExporter(
                tc,
                {"org_1": jira},
                state,
                today=TODAY,
            ).run()

        self.assertEqual(len(jira.created), 1)

    def test_manual_backfill_preserves_existing_incremental_cursor(self):
        state = self.new_state("2026-08-07")
        tc = FakeTimeCampClient()
        jira = FakeJiraClient()

        result = JiraTimeEntryExporter(
            tc,
            {"org_1": jira},
            state,
            today=TODAY,
        ).run(date(2026, 7, 1), date(2026, 7, 31))

        self.assertTrue(result.successful)
        self.assertEqual(
            JiraExportState.load(self.state_path).cursor_date,
            "2026-08-07",
        )


class JiraExportStateTest(unittest.TestCase):
    def test_loads_case_insensitive_personal_jira_api_tokens(self):
        tokens = load_jira_user_api_tokens(
            '{"Ada@Example.com":" personal-token "}'
        )

        self.assertEqual(
            tokens,
            {"ada@example.com": {"*": "personal-token"}},
        )

    def test_loads_different_tokens_for_each_jira_instance(self):
        tokens = load_jira_user_api_tokens(
            json.dumps(
                {
                    "ada@example.com": {
                        "https://one.atlassian.net/": " token-one ",
                        "https://TWO.atlassian.net": "token-two",
                    }
                }
            )
        )

        self.assertEqual(
            tokens,
            {
                "ada@example.com": {
                    "https://one.atlassian.net": "token-one",
                    "https://two.atlassian.net": "token-two",
                }
            },
        )

    def test_rejects_invalid_personal_jira_api_token_config(self):
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            load_jira_user_api_tokens('[{"email":"ada@example.com"}]')

        with self.assertRaisesRegex(ValueError, "empty API token"):
            load_jira_user_api_tokens('{"ada@example.com":" "}')

    @patch("export_time_entries_jira.JiraClient")
    def test_filtered_export_uses_matching_personal_jira_token(self, jira_client):
        instances = [
            {
                "instance_id": "org_1",
                "url": "https://jira.example.com",
                "email": "root@example.com",
                "token": "root-token",
            }
        ]

        clients = build_jira_clients(
            instances,
            user_email="Ada@Example.com",
            user_api_tokens={"ada@example.com": {"*": "personal-token"}},
        )

        jira_client.assert_called_once_with(
            "https://jira.example.com",
            "Ada@Example.com",
            "personal-token",
        )
        self.assertIs(clients["org_1"], jira_client.return_value)

    @patch("export_time_entries_jira.JiraClient")
    def test_filtered_export_falls_back_to_root_jira_credentials(self, jira_client):
        instances = [
            {
                "instance_id": "org_1",
                "url": "https://jira.example.com",
                "email": "root@example.com",
                "token": "root-token",
            }
        ]

        build_jira_clients(
            instances,
            user_email="missing@example.com",
            user_api_tokens={"ada@example.com": {"*": "personal-token"}},
        )

        jira_client.assert_called_once_with(
            "https://jira.example.com",
            "root@example.com",
            "root-token",
        )

    @patch("export_time_entries_jira.JiraClient")
    def test_filtered_export_selects_token_per_instance(self, jira_client):
        instances = [
            {
                "instance_id": "org_1",
                "url": "https://one.atlassian.net/",
                "email": "root-one@example.com",
                "token": "root-one",
            },
            {
                "instance_id": "org_2",
                "url": "https://two.atlassian.net",
                "email": "root-two@example.com",
                "token": "root-two",
            },
            {
                "instance_id": "org_3",
                "url": "https://three.atlassian.net",
                "email": "root-three@example.com",
                "token": "root-three",
            },
        ]

        build_jira_clients(
            instances,
            user_email="ada@example.com",
            user_api_tokens={
                "ada@example.com": {
                    "https://one.atlassian.net": "personal-one",
                    "https://two.atlassian.net": "personal-two",
                }
            },
        )

        self.assertEqual(
            jira_client.call_args_list,
            [
                call(
                    "https://one.atlassian.net/",
                    "ada@example.com",
                    "personal-one",
                ),
                call(
                    "https://two.atlassian.net",
                    "ada@example.com",
                    "personal-two",
                ),
                call(
                    "https://three.atlassian.net",
                    "root-three@example.com",
                    "root-three",
                ),
            ],
        )

    def test_loads_v1_state_and_writes_universal_v2_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cursor_date": "2026-08-01",
                        "entries": {
                            "101": {
                                "instance_id": "org_1",
                                "issue_key": "TCD-123",
                                "worklog_id": "5001",
                                "source_fingerprint": "v1:abc",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            state = JiraExportState.load(path)
            self.assertEqual(state.get("101").issue_key, "TCD-123")
            state.save()
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["version"], 2)
            self.assertEqual(migrated["adapter"], "jira")
            self.assertEqual(migrated["entries"]["101"]["remote_id"], "5001")

    def test_rejects_unknown_state_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(
                json.dumps({"version": 999, "cursor_date": None, "entries": {}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Unsupported Jira export state"):
                JiraExportState.load(path)

    def test_cli_writes_by_default_and_supports_dry_run(self):
        self.assertFalse(parse_args([]).dry_run)
        self.assertTrue(parse_args(["--dry-run"]).dry_run)

    def test_cli_accepts_user_email_and_builds_separate_state_name(self):
        args = parse_args(
            [
                "--env-file",
                "/tmp/export.env",
                "--user-email",
                "Ezee+Jira@PhreeTech.com",
            ]
        )

        self.assertEqual(args.env_file, "/tmp/export.env")
        self.assertEqual(args.user_email, "Ezee+Jira@PhreeTech.com")
        self.assertEqual(
            filtered_state_file(args.user_email),
            "data/jira_time_entries_state.ezee_jira_phreetech_com.json",
        )

    def test_finds_timecamp_user_by_case_insensitive_exact_email(self):
        user_id = find_timecamp_user_id(
            [
                {"user_id": "7", "email": "other@example.com"},
                {"user_id": "8", "email": "Ezee@PhreeTech.com"},
            ],
            "ezee@phreetech.com",
        )

        self.assertEqual(user_id, 8)


if __name__ == "__main__":
    unittest.main()

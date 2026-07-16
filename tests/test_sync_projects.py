import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import sync_projects
from src.mandatory_tags import MandatoryTagSyncResult, TagDefinition


class SyncProjectsCliTest(unittest.TestCase):
    def test_main_uses_input_file_for_preview_and_sync(self):
        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "TIMECAMP_STRICT_USER_SYNC", None),
            patch.object(sync_projects, "get_enabled_sync_actions", return_value={"tasks"}),
            patch.object(sync_projects, "print_sync_action_plan"),
            patch.object(sync_projects, "show_sync_preview") as show_sync_preview,
            patch.object(sync_projects, "sync_hierarchical_tasks_to_timecamp") as sync_tasks,
        ):
            sync_projects.main(["--input", "task_tc.json"])

        show_sync_preview.assert_called_once_with("task_tc.json")
        sync_tasks.assert_called_once_with({"tasks"}, "task_tc.json", False)

    def test_main_passes_cli_strict_user_sync_to_sync(self):
        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "TIMECAMP_STRICT_USER_SYNC", None),
            patch.object(sync_projects, "get_enabled_sync_actions", return_value={"users"}),
            patch.object(sync_projects, "print_sync_action_plan"),
            patch.object(sync_projects, "show_sync_preview") as show_sync_preview,
            patch.object(sync_projects, "sync_hierarchical_tasks_to_timecamp") as sync_tasks,
        ):
            sync_projects.main(["--input", "task_tc.json", "--strict-user-sync"])

        show_sync_preview.assert_called_once_with("task_tc.json")
        sync_tasks.assert_called_once_with({"users"}, "task_tc.json", True)

    def test_env_strict_user_sync_is_truthy(self):
        with patch.object(sync_projects, "TIMECAMP_STRICT_USER_SYNC", "true"):
            self.assertTrue(sync_projects.get_strict_user_sync_enabled(False))

    def test_show_sync_preview_limits_hierarchy_output_to_50_tasks(self):
        tasks = [
            {
                "task_id": f"task_{index}",
                "parent_id": 0,
                "name": f"Task {index}",
            }
            for index in range(55)
        ]

        with patch.object(sync_projects, "load_tasks_from_json", return_value=tasks):
            output = io.StringIO()
            with redirect_stdout(output):
                sync_projects.show_sync_preview("task_tc.json")

        lines = output.getvalue().splitlines()
        hierarchy_lines = [line for line in lines if "(ID: task_" in line]

        self.assertEqual(len(hierarchy_lines), 50)
        self.assertIn("Would sync 55 total tasks:", lines)
        self.assertIn("[L0] Task 49 (ID: task_49)", lines)
        self.assertNotIn("[L0] Task 50 (ID: task_50)", lines)
        self.assertIn("... hierarchy preview truncated after 50 task(s)", lines)

    def test_sync_logs_task_processing_plan_and_progress(self):
        class FakeClient:
            def __init__(self):
                self.counts = {}
                self.seconds = {}

            def _record_api_call(self, key):
                self.counts[key] = self.counts.get(key, 0) + 1
                self.seconds[key] = self.seconds.get(key, 0.0) + 1.0

            def get_api_metrics_snapshot(self):
                return {
                    "counts": dict(self.counts),
                    "seconds": dict(self.seconds),
                }

            def get_tasks(self):
                self._record_api_call("GET tasks")
                return [
                    {
                        "task_id": 123,
                        "external_task_id": "source_1",
                        "name": "Parent",
                        "users": {},
                    }
                ]

            def get_users(self):
                self._record_api_call("GET users")
                return [{"user_id": 1, "email": "one@example.com"}]

            def create_task(self, name, parent_id, external_task_id):
                self._record_api_call("POST tasks")
                return {
                    "task_id": 124,
                    "external_task_id": external_task_id,
                    "name": name,
                    "users": {},
                }

        source_tasks = [
            {
                "task_id": "source_1",
                "parent_id": 0,
                "name": "Parent",
                "external_task_id": "source_1",
                "assigned_users": {
                    "monday_user_1": {
                        "email": "one@example.com",
                        "username": "One User",
                    }
                },
            },
            {
                "task_id": "source_2",
                "parent_id": "source_1",
                "name": "Child",
                "external_task_id": "source_2",
            },
        ]
        user_sync_result = object()
        client = FakeClient()

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=source_tasks),
            patch.object(sync_projects, "TimeCampClient", return_value=client),
            patch.object(
                sync_projects,
                "build_assigned_user_sync_result",
                return_value=user_sync_result,
            ),
            patch.object(sync_projects, "sync_users_to_task") as sync_users_to_task,
        ):
            sync_users_to_task.return_value.assigned = 1
            sync_users_to_task.return_value.unassigned = 0
            output = io.StringIO()
            with redirect_stdout(output):
                sync_projects.sync_hierarchical_tasks_to_timecamp(
                    {"tasks", "users"},
                    "task_tc.json",
                    False,
                )

        lines = output.getvalue().splitlines()

        self.assertIn("Starting hierarchical task synchronization to TimeCamp...", lines)
        self.assertIn("Preparing task hierarchy and workload details...", lines)
        self.assertIn("- Source tasks sorted parent-before-child: 2 task(s) across 2 level(s)", lines)
        self.assertIn("- Existing TimeCamp source matches: 1", lines)
        self.assertIn("- Missing TimeCamp tasks: 1 (will create)", lines)
        self.assertIn("- Tasks with source assigned users: 1", lines)
        self.assertIn("Processing tasks in hierarchy order...", lines)
        self.assertIn(
            "API calls during setup/preflight: total=1; GET tasks=1 (1.00s)",
            lines,
        )
        self.assertIn(
            "Processed 2/2 task(s): created=1, existing=1, missing_skipped=0, "
            "mandatory_tags=0, mandatory_tag_cache_skips=0, "
            "users_assigned=1, users_unassigned=0, "
            "api_calls=1, elapsed=0.00s",
            lines,
        )
        self.assertIn("Task processing loop completed in 0.00s", lines)
        self.assertIn(
            "API calls during task loop: total=1; POST tasks=1 (1.00s)",
            lines,
        )

    def test_estimate_sync_updates_only_changed_bulk_loaded_budgets(self):
        class FakeClient:
            def __init__(self):
                self.counts = {}
                self.seconds = {}
                self.estimate_updates = []

            def _record_api_call(self, key):
                self.counts[key] = self.counts.get(key, 0) + 1
                self.seconds[key] = self.seconds.get(key, 0.0) + 1.0

            def get_api_metrics_snapshot(self):
                return {
                    "counts": dict(self.counts),
                    "seconds": dict(self.seconds),
                }

            def get_tasks(self):
                self._record_api_call("GET tasks")
                return [
                    {
                        "task_id": 101,
                        "external_task_id": "source_current",
                        "budgeted": 7200,
                        "budget_unit": "hours",
                    },
                    {
                        "task_id": 102,
                        "external_task_id": "source_changed",
                        "budgeted": 0,
                        "budget_unit": "hours",
                    },
                    {
                        "task_id": 103,
                        "external_task_id": "source_without_estimate",
                        "budgeted": 3600,
                        "budget_unit": "hours",
                    },
                ]

            def update_task_estimate(self, task_id, estimate_seconds):
                self._record_api_call("PATCH v3/task/{id}/billing-settings")
                self.estimate_updates.append((task_id, estimate_seconds))
                return {}

        source_tasks = [
            {
                "task_id": "source_current",
                "external_task_id": "source_current",
                "parent_id": 0,
                "name": "Already current",
                "original_estimate_seconds": 7200,
            },
            {
                "task_id": "source_changed",
                "external_task_id": "source_changed",
                "parent_id": 0,
                "name": "Needs update",
                "original_estimate_seconds": 2700,
            },
            {
                "task_id": "source_without_estimate",
                "external_task_id": "source_without_estimate",
                "parent_id": 0,
                "name": "Leave manual estimate alone",
                "original_estimate_seconds": None,
            },
        ]
        client = FakeClient()

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=source_tasks),
            patch.object(sync_projects, "TimeCampClient", return_value=client),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                sync_projects.sync_hierarchical_tasks_to_timecamp(
                    {"estimates"},
                    "tasks.json",
                    False,
                )

        self.assertEqual(client.estimate_updates, [(102, 2700)])
        self.assertIn("- Estimates updated: 1", output.getvalue())
        self.assertIn("- Estimates already current: 1", output.getvalue())
        self.assertIn(
            "API calls during task loop: total=1; "
            "PATCH v3/task/{id}/billing-settings=1 (1.00s)",
            output.getvalue(),
        )

    def test_name_sync_updates_only_real_mismatches_and_is_idempotent(self):
        class FakeClient:
            def __init__(self):
                self.counts = {}
                self.seconds = {}
                self.name_updates = []
                self.tasks = [
                    {
                        "task_id": 101,
                        "external_task_id": "source_current",
                        "name": "[TCD-1] Already current",
                    },
                    {
                        "task_id": 102,
                        "external_task_id": "source_changed",
                        "name": "Old task name",
                    },
                    {
                        "task_id": 103,
                        "external_task_id": "source_sanitized",
                        "name": "[TCD-3] " + ("x" * 182),
                    },
                ]

            def _record_api_call(self, key):
                self.counts[key] = self.counts.get(key, 0) + 1
                self.seconds[key] = self.seconds.get(key, 0.0) + 1.0

            def get_api_metrics_snapshot(self):
                return {
                    "counts": dict(self.counts),
                    "seconds": dict(self.seconds),
                }

            def get_tasks(self):
                self._record_api_call("GET tasks")
                return self.tasks

            def update_task_name(self, task_id, name):
                self._record_api_call("PUT tasks")
                self.name_updates.append((task_id, name))
                return {}

        source_tasks = [
            {
                "task_id": "source_current",
                "external_task_id": "source_current",
                "parent_id": 0,
                "name": "[TCD-1] Already current",
            },
            {
                "task_id": "source_changed",
                "external_task_id": "source_changed",
                "parent_id": 0,
                "name": " [TCD-2] New | task → name ",
            },
            {
                "task_id": "source_sanitized",
                "external_task_id": "source_sanitized",
                "parent_id": 0,
                "name": "  [TCD-3]\t" + ("x" * 200),
            },
        ]
        client = FakeClient()

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=source_tasks),
            patch.object(sync_projects, "TimeCampClient", return_value=client),
        ):
            first_output = io.StringIO()
            with redirect_stdout(first_output):
                sync_projects.sync_hierarchical_tasks_to_timecamp(
                    {"names"},
                    "tasks.json",
                    False,
                )

            second_output = io.StringIO()
            with redirect_stdout(second_output):
                sync_projects.sync_hierarchical_tasks_to_timecamp(
                    {"names"},
                    "tasks.json",
                    False,
                )

        self.assertEqual(client.name_updates, [(102, "[TCD-2] New  task  name")])
        self.assertIn("- Task name updates needed: 1", first_output.getvalue())
        self.assertIn("- Names updated: 1", first_output.getvalue())
        self.assertIn("- Names already current: 2", first_output.getvalue())
        self.assertIn("- Task name updates needed: 0", second_output.getvalue())
        self.assertIn("- Names updated: 0", second_output.getvalue())
        self.assertIn("- Names already current: 3", second_output.getvalue())
        self.assertIn(
            "API calls during task loop: no tracked API calls",
            second_output.getvalue(),
        )

    def test_new_task_uses_timecamp_normalized_name(self):
        class FakeClient:
            def __init__(self):
                self.created_names = []

            def get_tasks(self):
                return []

            def get_api_metrics_snapshot(self):
                return {"counts": {}, "seconds": {}}

            def create_task(self, name, parent_id, external_task_id):
                self.created_names.append(name)
                return {
                    "task_id": 201,
                    "external_task_id": external_task_id,
                    "name": name,
                }

        source_task = {
            "task_id": "source_new",
            "external_task_id": "source_new",
            "parent_id": 0,
            "name": " New | task → name ",
        }
        client = FakeClient()

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
            patch.object(sync_projects, "TimeCampClient", return_value=client),
        ):
            sync_projects.sync_hierarchical_tasks_to_timecamp(
                {"tasks"},
                "tasks.json",
                False,
            )

        self.assertEqual(client.created_names, ["New  task  name"])

    def test_new_task_gets_source_estimate_after_creation(self):
        class FakeClient:
            def __init__(self):
                self.estimate_updates = []

            def get_tasks(self):
                return []

            def get_api_metrics_snapshot(self):
                return {"counts": {}, "seconds": {}}

            def create_task(self, name, parent_id, external_task_id):
                return {
                    "task_id": 201,
                    "external_task_id": external_task_id,
                    "name": name,
                }

            def update_task_estimate(self, task_id, estimate_seconds):
                self.estimate_updates.append((task_id, estimate_seconds))
                return {}

        source_task = {
            "task_id": "source_new",
            "external_task_id": "source_new",
            "parent_id": 0,
            "name": "New estimated task",
            "original_estimate_seconds": 5400,
        }
        client = FakeClient()

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
            patch.object(sync_projects, "TimeCampClient", return_value=client),
        ):
            sync_projects.sync_hierarchical_tasks_to_timecamp(
                {"tasks", "estimates"},
                "tasks.json",
                False,
            )

        self.assertEqual(client.estimate_updates, [(201, 5400)])

    def test_strict_user_sync_skips_tasks_with_no_source_or_current_users(self):
        class FakeClient:
            def get_tasks(self):
                return [
                    {
                        "task_id": 123,
                        "external_task_id": "source_1",
                        "name": "Project",
                        "users": {},
                    }
                ]

        source_task = {
            "task_id": "source_1",
            "parent_id": 0,
            "name": "Project",
            "external_task_id": "source_1",
        }
        user_sync_result = object()

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
            patch.object(sync_projects, "TimeCampClient", return_value=FakeClient()),
            patch.object(
                sync_projects,
                "build_assigned_user_sync_result",
                return_value=user_sync_result,
            ),
            patch.object(sync_projects, "sync_users_to_task") as sync_users_to_task,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                sync_projects.sync_hierarchical_tasks_to_timecamp(
                    {"users"},
                    "task_tc.json",
                    True,
                )

        sync_users_to_task.assert_not_called()
        self.assertIn(
            "- User sync skipped because no source/current users: 1",
            output.getvalue().splitlines(),
        )
        self.assertIn(
            "- Strict user sync candidates: 0 task(s)",
            output.getvalue().splitlines(),
        )

    def test_strict_user_sync_runs_for_tasks_without_assigned_users(self):
        class FakeClient:
            def get_tasks(self):
                return [
                    {
                        "task_id": 123,
                        "external_task_id": "source_1",
                        "name": "Project",
                        "users": {
                            "20": {"user_id": "20", "role_id": "3"},
                        },
                    }
                ]

        source_task = {
            "task_id": "source_1",
            "parent_id": 0,
            "name": "Project",
            "external_task_id": "source_1",
        }
        user_sync_result = object()

        client = FakeClient()

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
            patch.object(sync_projects, "TimeCampClient", return_value=client),
            patch.object(
                sync_projects,
                "build_assigned_user_sync_result",
                return_value=user_sync_result,
            ),
            patch.object(sync_projects, "sync_users_to_task") as sync_users_to_task,
        ):
            sync_users_to_task.return_value.assigned = 0
            sync_users_to_task.return_value.unassigned = 2

            sync_projects.sync_hierarchical_tasks_to_timecamp(
                {"users"},
                "task_tc.json",
                True,
            )

        sync_users_to_task.assert_called_once_with(
            client=client,
            timecamp_task_id=123,
            source_task=source_task,
            user_sync_result=user_sync_result,
            strict=True,
            current_assigned_users={
                "20": {"user_id": "20", "role_id": "3"},
            },
        )

    def test_strict_user_sync_makes_no_user_api_calls_when_current_users_match(self):
        class FakeClient:
            def __init__(self):
                self.counts = {}
                self.seconds = {}

            def _record_api_call(self, key):
                self.counts[key] = self.counts.get(key, 0) + 1
                self.seconds[key] = self.seconds.get(key, 0.0) + 1.0

            def get_api_metrics_snapshot(self):
                return {
                    "counts": dict(self.counts),
                    "seconds": dict(self.seconds),
                }

            def get_tasks(self):
                self._record_api_call("GET tasks")
                return [
                    {
                        "task_id": 123,
                        "external_task_id": "source_1",
                        "name": "Project",
                        "users": {
                            "10": {"user_id": "10", "role_id": "3"},
                        },
                    }
                ]

            def get_users(self):
                self._record_api_call("GET users")
                return [{"user_id": 10, "email": "keep@example.com"}]

            def assign_users_to_task(self, task_id, user_ids, role_id):
                raise AssertionError("matching users should not be assigned again")

            def get_project_assigned_users(self, task_id):
                raise AssertionError("main sync should use users from /tasks")

            def unassign_users_from_task(self, task_id, user_ids):
                raise AssertionError("matching users should not be unassigned")

        source_task = {
            "task_id": "source_1",
            "parent_id": 0,
            "name": "Project",
            "external_task_id": "source_1",
            "assigned_users": {
                "monday_user_1": {
                    "email": "keep@example.com",
                    "username": "Keep User",
                }
            },
        }

        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
            patch.object(sync_projects, "TimeCampClient", return_value=FakeClient()),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                sync_projects.sync_hierarchical_tasks_to_timecamp(
                    {"users"},
                    "task_tc.json",
                    True,
                )

        lines = output.getvalue().splitlines()

        self.assertIn("users_assigned=0", output.getvalue())
        self.assertIn("users_unassigned=0", output.getvalue())
        self.assertIn("api_calls=0", output.getvalue())
        self.assertIn("API calls during task loop: no tracked API calls", lines)

    def test_mandatory_tag_cache_skips_current_task_tag_lookup(self):
        class FakeClient:
            def __init__(self):
                self.counts = {}
                self.seconds = {}
                self.task_tag_calls = []

            def get_api_metrics_snapshot(self):
                return {
                    "counts": dict(self.counts),
                    "seconds": dict(self.seconds),
                }

            def get_tasks(self):
                return [
                    {
                        "task_id": 123,
                        "external_task_id": "source_1",
                        "name": "Project",
                        "modify_time": "2026-07-09 10:00:00",
                        "users": {},
                    }
                ]

            def get_task_tags(self, task_id):
                self.task_tag_calls.append(task_id)
                return {}

        source_task = {
            "task_id": "source_1",
            "parent_id": 0,
            "name": "Project",
            "external_task_id": "source_1",
            "mandatory_tags": {"Client": ["Acme"]},
        }
        tag_sync = MandatoryTagSyncResult(
            tags={("client", "acme"): TagDefinition(tag_list_id=1, tag_id=10)}
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "mandatory_tag_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "tasks": {
                            "source_1": {
                                "timecamp_task_id": "123",
                                "timecamp_modify_time": "2026-07-09 10:00:00",
                                "desired_tag_assignments": [
                                    {
                                        "tag_list_id": 1,
                                        "tag_id": 10,
                                        "mandatory": True,
                                    }
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            client = FakeClient()

            with (
                patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
                patch.object(sync_projects, "TIMECAMP_MANDATORY_TAG_CACHE_FILE", str(cache_path), create=True),
                patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
                patch.object(sync_projects, "TimeCampClient", return_value=client),
                patch.object(sync_projects, "ensure_mandatory_tags", return_value=tag_sync),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    sync_projects.sync_hierarchical_tasks_to_timecamp(
                        {"mandatory_tags"},
                        "task_tc.json",
                        False,
                    )

        self.assertEqual(client.task_tag_calls, [])
        self.assertIn("mandatory_tag_cache_skips=1", output.getvalue())
        self.assertIn("API calls during task loop: no tracked API calls", output.getvalue())

    def test_mandatory_tag_cache_records_noop_checked_task(self):
        class FakeClient:
            def __init__(self):
                self.counts = {}
                self.seconds = {}

            def _record_api_call(self, key):
                self.counts[key] = self.counts.get(key, 0) + 1
                self.seconds[key] = self.seconds.get(key, 0.0) + 1.0

            def get_api_metrics_snapshot(self):
                return {
                    "counts": dict(self.counts),
                    "seconds": dict(self.seconds),
                }

            def get_tasks(self):
                return [
                    {
                        "task_id": 123,
                        "external_task_id": "source_1",
                        "name": "Project",
                        "modify_time": "2026-07-09 10:00:00",
                        "users": {},
                    }
                ]

            def get_task_tags(self, task_id):
                self._record_api_call("GET task/{id}/tags")
                return {
                    "1": {
                        "id": "1",
                        "name": "Client",
                        "inherit": 0,
                        "hasAssignedTags": True,
                        "tags": [
                            {
                                "id": "10",
                                "name": "Acme",
                                "mandatory": "1",
                                "inherit": 0,
                            }
                        ],
                    }
                }

        source_task = {
            "task_id": "source_1",
            "parent_id": 0,
            "name": "Project",
            "external_task_id": "source_1",
            "mandatory_tags": {"Client": ["Acme"]},
        }
        tag_sync = MandatoryTagSyncResult(
            tags={("client", "acme"): TagDefinition(tag_list_id=1, tag_id=10)}
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "mandatory_tag_cache.json"
            with (
                patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
                patch.object(sync_projects, "TIMECAMP_MANDATORY_TAG_CACHE_FILE", str(cache_path), create=True),
                patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
                patch.object(sync_projects, "TimeCampClient", return_value=FakeClient()),
                patch.object(sync_projects, "ensure_mandatory_tags", return_value=tag_sync),
            ):
                sync_projects.sync_hierarchical_tasks_to_timecamp(
                    {"mandatory_tags"},
                    "task_tc.json",
                    False,
                )

            cache = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(
            cache["tasks"]["source_1"],
            {
                "timecamp_task_id": "123",
                "timecamp_modify_time": "2026-07-09 10:00:00",
                "desired_tag_assignments": [
                    {
                        "tag_list_id": 1,
                        "tag_id": 10,
                        "mandatory": True,
                    }
                ],
            },
        )

    def test_mandatory_tags_use_internal_bulk_project_tags_on_cache_miss(self):
        class FakeClient:
            def __init__(self):
                self.counts = {}
                self.seconds = {}
                self.task_tag_calls = []

            def _record_api_call(self, key):
                self.counts[key] = self.counts.get(key, 0) + 1
                self.seconds[key] = self.seconds.get(key, 0.0) + 1.0

            def get_api_metrics_snapshot(self):
                return {
                    "counts": dict(self.counts),
                    "seconds": dict(self.seconds),
                }

            def get_tasks(self):
                return [
                    {
                        "task_id": 123,
                        "external_task_id": "source_1",
                        "name": "Project",
                        "parent_id": 999,
                        "modify_time": "2026-07-09 10:00:00",
                        "users": {},
                    }
                ]

            def get_internal_projects(self, parent_id, status="active", include=None, page=1):
                self._record_api_call("POST internal/v3/projects")
                self.assertEqual(parent_id, 999)
                self.assertEqual(status, "active")
                self.assertEqual(include, ["tags"])
                self.assertEqual(page, 1)
                return {
                    "data": [
                        {
                            "taskId": 123,
                            "tags": {
                                "10": {
                                    "mandatory": 1,
                                    "tagListId": "1",
                                }
                            },
                            "tag_lists": {
                                "1": 0,
                            },
                        }
                    ],
                    "pagination": {
                        "page": 1,
                        "totalPages": 1,
                    },
                }

            def get_task_tags(self, task_id):
                self.task_tag_calls.append(task_id)
                raise AssertionError("should use internal bulk project tags")

        source_task = {
            "task_id": "source_1",
            "parent_id": 0,
            "name": "Project",
            "external_task_id": "source_1",
            "mandatory_tags": {"Client": ["Acme"]},
        }
        tag_sync = MandatoryTagSyncResult(
            tags={("client", "acme"): TagDefinition(tag_list_id=1, tag_id=10)}
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "mandatory_tag_cache.json"
            client = FakeClient()
            with (
                patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
                patch.object(sync_projects, "TIMECAMP_MANDATORY_TAG_CACHE_FILE", str(cache_path), create=True),
                patch.object(sync_projects, "load_tasks_from_json", return_value=[source_task]),
                patch.object(sync_projects, "TimeCampClient", return_value=client),
                patch.object(sync_projects, "ensure_mandatory_tags", return_value=tag_sync),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    sync_projects.sync_hierarchical_tasks_to_timecamp(
                        {"mandatory_tags"},
                        "task_tc.json",
                        False,
                    )

        self.assertEqual(client.task_tag_calls, [])
        self.assertIn("API calls during task loop: total=1; POST internal/v3/projects=1", output.getvalue())


if __name__ == "__main__":
    unittest.main()

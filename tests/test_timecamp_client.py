import unittest

from src.timecamp_client import TimeCampClient, normalize_timecamp_task_name


class TimeCampClientTest(unittest.TestCase):
    def test_normalize_timecamp_task_name_matches_stored_representation(self):
        source_name = "  Alpha\t| Beta → Gamma  " + ("x" * 200)

        self.assertEqual(
            normalize_timecamp_task_name(source_name),
            ("Alpha  Beta  Gamma  " + ("x" * 200))[:190],
        )

    def test_normalize_timecamp_task_name_handles_none(self):
        self.assertEqual(normalize_timecamp_task_name(None), "")

    def test_request_records_api_metrics_with_normalized_endpoint(self):
        class FakeResponse:
            status_code = 200
            headers = {}
            content = b'{"data":"ok"}'

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": "ok"}

        class FakeSession:
            def __init__(self):
                self.headers = {}

            def request(self, method, url, json=None, params=None):
                return FakeResponse()

        client = TimeCampClient("token", base_url="https://example.test")
        client.session = FakeSession()

        response = client._request(
            "PUT",
            "v3/projects/123/unassign",
            json={"userIds": [456]},
        )

        self.assertEqual(response, {"data": "ok"})
        self.assertEqual(
            client.get_api_metrics_snapshot()["counts"],
            {"PUT v3/projects/{id}/unassign": 1},
        )

    def test_get_project_assigned_users_uses_v3_endpoint(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "json": json,
                        "params": params,
                    }
                )
                return {"data": [{"userId": 123, "taskId": 456}]}

        response = FakeClient().get_project_assigned_users(456)

        self.assertEqual(response, [{"userId": 123, "taskId": 456}])
        self.assertEqual(
            calls,
            [
                {
                    "method": "GET",
                    "endpoint": "v3/projects/456/assigned-users",
                    "json": None,
                    "params": None,
                }
            ],
        )

    def test_unassign_users_from_task_uses_v3_endpoint(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "json": json,
                        "params": params,
                    }
                )
                return {"data": "ok"}

        response = FakeClient().unassign_users_from_task(456, [123, 124])

        self.assertEqual(response, {"data": "ok"})
        self.assertEqual(
            calls,
            [
                {
                    "method": "PUT",
                    "endpoint": "v3/projects/456/unassign",
                    "json": {"userIds": [123, 124]},
                    "params": None,
                }
            ],
        )

    def test_update_task_estimate_uses_v3_billing_settings_endpoint(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "json": json,
                        "params": params,
                    }
                )
                return {"data": {"budget": 2700, "budgetUnit": "hours"}}

        response = FakeClient().update_task_estimate(456, 2700)

        self.assertEqual(
            response,
            {"data": {"budget": 2700, "budgetUnit": "hours"}},
        )
        self.assertEqual(
            calls,
            [
                {
                    "method": "PATCH",
                    "endpoint": "v3/task/456/billing-settings",
                    "json": {"budget": 2700, "budgetUnit": "hours"},
                    "params": None,
                }
            ],
        )

    def test_update_task_name_sends_only_task_id_and_changed_name(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "json": json,
                        "params": params,
                    }
                )
                return {"task_id": "456", "name": "[TCD-123] Task name"}

        response = FakeClient().update_task_name(456, "[TCD-123] Task name")

        self.assertEqual(
            response,
            {"task_id": "456", "name": "[TCD-123] Task name"},
        )
        self.assertEqual(
            calls,
            [
                {
                    "method": "PUT",
                    "endpoint": "tasks",
                    "json": {
                        "task_id": 456,
                        "name": "[TCD-123] Task name",
                    },
                    "params": None,
                }
            ],
        )

    def test_update_time_entry_task_uses_v3_endpoint(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "json": json,
                        "params": params,
                    }
                )
                return {"data": {"entry_id": 123, "task_id": 456}}

        response = FakeClient().update_time_entry_task(123, 456)

        self.assertEqual(response, {"data": {"entry_id": 123, "task_id": 456}})
        self.assertEqual(
            calls,
            [
                {
                    "method": "PUT",
                    "endpoint": "v3/time-entries/123",
                    "json": {"taskId": 456},
                    "params": None,
                }
            ],
        )

    def test_get_time_entries_supports_modification_dates_without_entry_dates(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append((method, endpoint, params))
                return [{"id": 123}]

        response = FakeClient().get_time_entries(
            modify_from="2026-08-01",
            modify_to="2026-08-10",
        )

        self.assertEqual(response, [{"id": 123}])
        self.assertEqual(
            calls,
            [
                (
                    "GET",
                    "entries",
                    {
                        "modify_from": "2026-08-01",
                        "modify_to": "2026-08-10",
                    },
                )
            ],
        )

    def test_get_time_entry_deletions_uses_modification_window(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append((method, endpoint, params))
                return [{"entry_id": 123}]

        response = FakeClient().get_time_entry_deletions(
            "2026-08-01",
            "2026-08-10",
            user_ids=[1, 2],
        )

        self.assertEqual(response, [{"entry_id": 123}])
        self.assertEqual(
            calls,
            [
                (
                    "GET",
                    "entries_deletions",
                    {
                        "from": "2026-08-01",
                        "to": "2026-08-10",
                        "user_ids": "1,2",
                    },
                )
            ],
        )

    def test_get_user_details_batches_and_normalizes_single_user_response(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request(self, method, endpoint, json=None, params=None):
                calls.append(params["user_id"])
                if params["user_id"] == "1,2":
                    return [{"user_id": 1}, {"user_id": 2}]
                return {"user_id": 3}

        response = FakeClient().get_user_details([1, 2, 3, 1], batch_size=2)

        self.assertEqual(calls, ["1,2", "3"])
        self.assertEqual(
            response,
            [{"user_id": 1}, {"user_id": 2}, {"user_id": 3}],
        )

    def test_get_time_entries_requires_complete_date_pairs(self):
        client = object.__new__(TimeCampClient)

        with self.assertRaisesRegex(ValueError, "Both start_date and end_date"):
            client.get_time_entries(start_date="2026-08-01")

        with self.assertRaisesRegex(ValueError, "Both modify_from and modify_to"):
            client.get_time_entries(modify_from="2026-08-01")

    def test_get_internal_projects_posts_parent_status_and_include(self):
        calls = []

        class FakeClient(TimeCampClient):
            def __init__(self):
                pass

            def _request_internal(self, method, endpoint, json=None, params=None):
                calls.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "json": json,
                        "params": params,
                    }
                )
                return {"data": [{"taskId": 123}], "pagination": {"page": 2}}

        response = FakeClient().get_internal_projects(
            parent_id=456,
            status="active",
            include=["tags"],
            page=2,
        )

        self.assertEqual(response, {"data": [{"taskId": 123}], "pagination": {"page": 2}})
        self.assertEqual(
            calls,
            [
                {
                    "method": "POST",
                    "endpoint": "v3/projects",
                    "json": {
                        "parentId": 456,
                        "status": "active",
                        "include": ["tags"],
                        "page": 2,
                    },
                    "params": None,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()

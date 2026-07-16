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

import os
import unittest

from fetch_timecamp import TimeCampFetcher, build_task_structure


class FetchTimeCampTest(unittest.TestCase):
    def test_fetcher_uses_fetch_token_env_var(self):
        previous_token = os.environ.get("TIMECAMP_API_TOKEN_FETCH")
        os.environ["TIMECAMP_API_TOKEN_FETCH"] = "fetch-token"
        created_clients = []

        class FakeClient:
            def __init__(self, api_token):
                created_clients.append(api_token)

            def get_tasks(self):
                return []

        try:
            fetcher = TimeCampFetcher(client_cls=FakeClient)
            self.assertEqual(created_clients, ["fetch-token"])
        finally:
            if previous_token is None:
                os.environ.pop("TIMECAMP_API_TOKEN_FETCH", None)
            else:
                os.environ["TIMECAMP_API_TOKEN_FETCH"] = previous_token

    def test_build_task_structure_preserves_included_timecamp_hierarchy(self):
        source_tasks = [
            {"task_id": "100", "name": "Client", "parent_id": "0"},
            {"task_id": "200", "name": "Project", "parent_id": "100"},
            {"task_id": "300", "name": "Archived", "parent_id": "100", "archived": 1},
            {"task_id": "400", "name": "Orphan", "parent_id": "300"},
        ]

        self.assertEqual(
            build_task_structure(source_tasks),
            [
                {
                    "name": "Client",
                    "task_id": "timecamp_100",
                    "parent_id": 0,
                    "timecamp_task_id": "100",
                },
                {
                    "name": "Project",
                    "task_id": "timecamp_200",
                    "parent_id": "timecamp_100",
                    "timecamp_task_id": "200",
                },
                {
                    "name": "Orphan",
                    "task_id": "timecamp_400",
                    "parent_id": 0,
                    "timecamp_task_id": "400",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()

import unittest

from fetch_netsuite import NetSuiteFetcher, build_task_structure


class FakeNetSuiteClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []

    def suiteql(self, query):
        self.queries.append(query)
        return self.responses.pop(0)


class FetchNetSuiteTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "classification": {
                "tag_list_name": "CAPEX / OPEX",
                "allowed_values": ["CAPEX", "OPEX"],
                "value_map": {"1": "CAPEX", "2": "OPEX"},
            }
        }

    def test_builds_hierarchy_classification_estimates_and_no_user_sync(self):
        projects = [
            {"ID": "10", "NAME": "Parent project", "capex_opex": "1"},
            {"id": "20", "name": "Child project", "parent_id": "10"},
            {"id": "99", "name": "Inactive", "is_inactive": "T"},
        ]
        project_tasks = [
            {
                "id": "100",
                "name": "Design",
                "project_id": "20",
                "estimated_work_hours": "1.5",
                "activity_id": "501",
            },
            {
                "id": "200",
                "name": "Delivery",
                "project_id": "20",
                "parent_id": "100",
                "capex_opex": "2",
            },
        ]

        tasks = build_task_structure(projects, project_tasks, self.config)

        self.assertEqual(
            [task["task_id"] for task in tasks],
            [
                "netsuite_project_10",
                "netsuite_project_20",
                "netsuite_project_task_100",
                "netsuite_project_task_200",
            ],
        )
        self.assertEqual(tasks[1]["parent_id"], "netsuite_project_10")
        self.assertEqual(tasks[2]["parent_id"], "netsuite_project_20")
        self.assertEqual(tasks[3]["parent_id"], "netsuite_project_task_100")
        self.assertEqual(tasks[2]["original_estimate_seconds"], 5400)
        self.assertEqual(tasks[2]["netsuite"]["activity_id"], "501")
        self.assertEqual(
            tasks[2]["mandatory_tags"],
            {"CAPEX / OPEX": ["CAPEX"]},
        )
        self.assertEqual(
            tasks[3]["mandatory_tags"],
            {"CAPEX / OPEX": ["OPEX"]},
        )
        self.assertTrue(
            all("assigned_users" not in task for task in tasks),
            "NetSuite import must not synchronize users",
        )

    def test_rejects_unknown_financial_classification(self):
        projects = [{"id": "10", "name": "Project", "capex_opex": "MAYBE"}]

        with self.assertRaisesRegex(ValueError, "Unknown CAPEX/OPEX"):
            build_task_structure(projects, [], self.config)

    def test_required_classification_rejects_unclassified_project(self):
        config = {
            "classification": {
                "required": True,
                "allowed_values": ["CAPEX", "OPEX"],
            }
        }

        with self.assertRaisesRegex(ValueError, "no required CAPEX/OPEX"):
            build_task_structure(
                [{"id": "10", "name": "Unclassified"}],
                [],
                config,
            )

    def test_rejects_project_task_without_active_project(self):
        with self.assertRaisesRegex(ValueError, "missing active project 10"):
            build_task_structure(
                [],
                [{"id": "100", "name": "Task", "project_id": "10"}],
                self.config,
            )

    def test_fetcher_executes_only_project_and_project_task_queries(self):
        config = {
            **self.config,
            "suiteql": {
                "projects": "projects query",
                "project_tasks": "tasks query",
            },
        }
        client = FakeNetSuiteClient(
            [
                [{"id": "10", "name": "Project"}],
                [{"id": "100", "name": "Task", "project_id": "10"}],
            ]
        )

        tasks = NetSuiteFetcher(config, client).fetch_all_data()

        self.assertEqual(client.queries, ["projects query", "tasks query"])
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all("assigned_users" not in task for task in tasks))


if __name__ == "__main__":
    unittest.main()

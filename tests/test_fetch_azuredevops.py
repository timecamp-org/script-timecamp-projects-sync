import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fetch_azuredevops
import sync_projects
from fetch_azuredevops import AzureDevOpsFetcher, generate_azure_devops_org_id

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

ACME_URL = "https://dev.azure.com/acme"
# TimeCamp tasks of existing customers carry this value. If it changes, the
# next sync archives every synced Azure DevOps task and creates it again.
ACME_ORG_ID = "org_955312"

AZURE_ENV = {
    "AZUREDEVOPS_INSTANCE1_NAME": "Acme",
    "AZUREDEVOPS_INSTANCE1_URL": ACME_URL,
    "AZUREDEVOPS_INSTANCE1_TOKEN": "token",
}
EPIC = {
    "id": 101,
    "title": "Checkout redesign",
    "state": "Active",
    "relations": [],
}

ORG_ID_IN_NEW_PROCESS = (
    "import json, sys\n"
    "from fetch_azuredevops import generate_azure_devops_org_id\n"
    "url = sys.argv[1]\n"
    "print(json.dumps([generate_azure_devops_org_id(url), hash(url)]))\n"
)
FETCH_IN_NEW_PROCESS = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from test_fetch_azuredevops import fetch_tasks\n"
    "print(json.dumps(fetch_tasks(json.loads(sys.argv[2]))))\n"
)


def story(state):
    return {
        "id": 102,
        "title": "Pay with card",
        "state": state,
        "relations": [
            {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"{ACME_URL}/_apis/wit/workItems/101",
            }
        ],
    }


class FakeAzureDevOpsClient:
    def __init__(self, work_items):
        self.work_items = work_items

    def get_projects(self):
        return [{"id": "project-guid", "name": "Website"}]

    def get_work_items_for_project(self, project_id):
        return self.work_items

    _extract_id_from_url = fetch_azuredevops.AzureDevOpsClient._extract_id_from_url


def fetch_tasks(work_items):
    """Run the real fetcher against a fake Azure DevOps organization."""
    with (
        patch.dict(os.environ, AZURE_ENV, clear=True),
        patch.object(
            fetch_azuredevops,
            "AzureDevOpsClient",
            lambda url, token: FakeAzureDevOpsClient(work_items),
        ),
        redirect_stdout(io.StringIO()),
    ):
        return AzureDevOpsFetcher().fetch_all_data()


def run_in_new_process(code, *args, hash_seed):
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.splitlines()[-1])


def fetch_in_new_process(work_items, hash_seed):
    return run_in_new_process(
        FETCH_IN_NEW_PROCESS,
        str(TESTS_DIR),
        json.dumps(work_items),
        hash_seed=hash_seed,
    )


class FakeTimeCampClient:
    """Keeps TimeCamp tasks between sync runs, like the real account."""

    def __init__(self):
        self.tasks = []
        self.start_run()

    def start_run(self):
        self.created_external_ids = []
        self.archived_external_ids = []
        self.restored_external_ids = []

    def get_tasks(self, include_archived=False):
        return [
            dict(task)
            for task in self.tasks
            if include_archived or not task["archived"]
        ]

    def get_api_metrics_snapshot(self):
        return {"counts": {}, "seconds": {}}

    def create_task(self, name, parent_id, external_task_id):
        task = {
            "task_id": 1000 + len(self.tasks),
            "parent_id": parent_id,
            "name": name,
            "external_task_id": external_task_id,
            "archived": 0,
        }
        self.tasks.append(task)
        self.created_external_ids.append(external_task_id)
        return dict(task)

    def archive_task(self, task_id):
        task = self._get_task(task_id)
        task["archived"] = 1
        self.archived_external_ids.append(task["external_task_id"])

    def restore_task(self, task_id):
        task = self._get_task(task_id)
        task["archived"] = 0
        self.restored_external_ids.append(task["external_task_id"])
        return {"task_id": task_id, "archived": 0}

    def _get_task(self, task_id):
        return next(task for task in self.tasks if task["task_id"] == task_id)


def sync_to_timecamp(client, tasks):
    client.start_run()
    with (
        patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
        patch.object(sync_projects, "TIMECAMP_TASK_ID", None),
        patch.object(sync_projects, "load_tasks_from_json", return_value=tasks),
        patch.object(sync_projects, "TimeCampClient", return_value=client),
        redirect_stdout(io.StringIO()),
    ):
        sync_projects.sync_hierarchical_tasks_to_timecamp(
            {"tasks", "archive"},
            "tasks.json",
            False,
        )


class GenerateAzureDevOpsOrgIdTest(unittest.TestCase):
    def test_org_id_keeps_its_published_value(self):
        self.assertEqual(generate_azure_devops_org_id(ACME_URL), ACME_ORG_ID)

    def test_org_id_ignores_case_surrounding_spaces_and_trailing_slash(self):
        for url in (
            "https://dev.azure.com/acme/",
            " https://dev.azure.com/acme ",
            "HTTPS://DEV.AZURE.COM/Acme",
        ):
            with self.subTest(url=url):
                self.assertEqual(generate_azure_devops_org_id(url), ACME_ORG_ID)

    def test_different_organizations_get_different_ids(self):
        self.assertNotEqual(
            generate_azure_devops_org_id("https://dev.azure.com/globex"),
            ACME_ORG_ID,
        )

    def test_org_id_is_same_in_processes_with_different_hash_seeds(self):
        results = [
            run_in_new_process(ORG_ID_IN_NEW_PROCESS, ACME_URL, hash_seed=seed)
            for seed in ("1", "2", "3")
        ]

        # Control: the seeds really change str hash(), which caused the bug.
        self.assertEqual(len({builtin_hash for _, builtin_hash in results}), 3)
        self.assertEqual([org_id for org_id, _ in results], [ACME_ORG_ID] * 3)


class AzureDevOpsFetcherTest(unittest.TestCase):
    def test_task_ids_start_with_stable_org_id(self):
        done_task = {**EPIC, "id": 103, "title": "Old work", "state": "Done"}

        tasks = fetch_tasks([EPIC, story("New"), done_task])

        self.assertEqual(
            tasks,
            [
                {"name": "Acme", "task_id": ACME_ORG_ID, "parent_id": 0},
                {
                    "name": "Website",
                    "task_id": f"{ACME_ORG_ID}_project-guid",
                    "parent_id": ACME_ORG_ID,
                },
                {
                    "name": "Checkout redesign",
                    "task_id": f"{ACME_ORG_ID}_101",
                    "parent_id": f"{ACME_ORG_ID}_project-guid",
                },
                {
                    "name": "Pay with card",
                    "task_id": f"{ACME_ORG_ID}_102",
                    "parent_id": f"{ACME_ORG_ID}_101",
                },
            ],
        )

    def test_instances_variable_keeps_full_urls(self):
        env = {
            "AZUREDEVOPS_INSTANCES": (
                "Acme:https://dev.azure.com/acme:token1, "
                "OnPrem:https://tfs.example.com:8080/tfs/Main:token2"
            )
        }

        with patch.dict(os.environ, env, clear=True):
            instances = AzureDevOpsFetcher().instances

        self.assertEqual(
            instances,
            [
                {"name": "Acme", "url": ACME_URL, "token": "token1"},
                {
                    "name": "OnPrem",
                    "url": "https://tfs.example.com:8080/tfs/Main",
                    "token": "token2",
                },
            ],
        )


class AzureDevOpsFetchAndSyncTest(unittest.TestCase):
    """Each fetch runs in a new process, like separate cron runs."""

    def test_next_fetch_and_sync_keeps_existing_timecamp_tasks(self):
        client = FakeTimeCampClient()

        sync_to_timecamp(
            client,
            fetch_in_new_process([EPIC, story("Active")], hash_seed="1"),
        )
        self.assertEqual(len(client.created_external_ids), 4)

        sync_to_timecamp(
            client,
            fetch_in_new_process([EPIC, story("Active")], hash_seed="2"),
        )
        self.assertEqual(client.created_external_ids, [])
        self.assertEqual(client.archived_external_ids, [])

    def test_work_item_back_from_done_restores_its_task(self):
        client = FakeTimeCampClient()
        sync_to_timecamp(
            client,
            fetch_in_new_process([EPIC, story("Active")], hash_seed="1"),
        )

        sync_to_timecamp(
            client,
            fetch_in_new_process([EPIC, story("Done")], hash_seed="2"),
        )
        self.assertEqual(client.archived_external_ids, [f"sync_{ACME_ORG_ID}_102"])

        sync_to_timecamp(
            client,
            fetch_in_new_process([EPIC, story("Active")], hash_seed="3"),
        )
        self.assertEqual(client.restored_external_ids, [f"sync_{ACME_ORG_ID}_102"])
        self.assertEqual(client.created_external_ids, [])
        self.assertEqual(client.archived_external_ids, [])


if __name__ == "__main__":
    unittest.main()

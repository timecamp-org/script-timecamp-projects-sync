import os
import unittest
from unittest.mock import patch

from fetch_clickup import (
    ClickUpFetcher,
    build_custom_fields_description,
    find_named_item,
)


class FakeClickUpClient:
    def get_workspaces(self):
        return [{"id": "w1", "name": "Example Workspace"}]

    def get_spaces(self, workspace_id):
        self.workspace_id = workspace_id
        return [{"id": "s1", "name": "Main Space"}]

    def get_folders(self, space_id):
        self.space_id = space_id
        return [{"id": "f1", "name": "Active Folder"}]

    def get_lists(self, folder_id):
        self.folder_id = folder_id
        return [{"id": "l1", "name": "Team / Projects"}]

    def get_tasks(self, list_id, include_closed=False):
        self.list_id = list_id
        self.include_closed = include_closed
        return [
            {
                "id": "task-1",
                "name": "Project Alpha",
                "parent": None,
                "assignees": [
                    {
                        "id": 42,
                        "email": "person@example.com",
                        "username": "Person",
                    }
                ],
                "custom_fields": [
                    {
                        "id": "cf-code",
                        "name": "Project code",
                        "value": "ACME",
                    },
                    {
                        "id": "cf-type",
                        "name": "Billing type",
                        "value": "opt-1",
                        "type_config": {
                            "options": [
                                {"id": "opt-1", "name": "Fixed price"}
                            ]
                        },
                    },
                    {
                        "id": "cf-owner",
                        "name": "Project owner",
                        "value": "Person",
                    },
                ],
            },
            {
                "id": "subtask-1",
                "name": "Ignored subitem",
                "parent": "task-1",
                "assignees": [],
                "custom_fields": [],
            },
        ]


class ClickUpFetcherTest(unittest.TestCase):
    def test_fetches_flat_projects_with_users_and_description(self):
        env = {
            "CLICKUP_WORKSPACE_NAME": "Example Workspace",
            "CLICKUP_SPACE_NAME": "Main Space",
            "CLICKUP_FOLDER_NAME": "Active Folder",
            "CLICKUP_LIST_NAME": "Team / Projects",
            "CLICKUP_DESCRIPTION_CUSTOM_FIELDS": (
                "cf-code, Billing type, cf-owner"
            ),
            "CLICKUP_INCLUDE_CLOSED": "false",
        }
        client = FakeClickUpClient()

        with patch.dict(os.environ, env, clear=True):
            fetcher = ClickUpFetcher(client=client)
            projects = fetcher.fetch_all_data()

        self.assertEqual(client.workspace_id, "w1")
        self.assertEqual(client.space_id, "s1")
        self.assertEqual(client.folder_id, "f1")
        self.assertEqual(client.list_id, "l1")
        self.assertFalse(client.include_closed)
        self.assertEqual(
            projects,
            [
                {
                    "name": "Project Alpha",
                    "task_id": "clickup_task-1",
                    "external_task_id": "clickup_task-1",
                    "parent_id": 0,
                    "assigned_users": {
                        "42": {
                            "email": "person@example.com",
                            "username": "Person",
                        }
                    },
                    "description": (
                        "ClickUp custom fields:\n"
                        "Project code: ACME\n"
                        "Billing type: Fixed price\n"
                        "Project owner: Person"
                    ),
                }
            ],
        )

    def test_custom_fields_are_optional(self):
        env = {
            "CLICKUP_WORKSPACE_NAME": "Example Workspace",
            "CLICKUP_SPACE_NAME": "Main Space",
            "CLICKUP_FOLDER_NAME": "Active Folder",
            "CLICKUP_LIST_NAME": "Team / Projects",
        }

        with patch.dict(os.environ, env, clear=True):
            projects = ClickUpFetcher(
                client=FakeClickUpClient()
            ).fetch_all_data()

        self.assertNotIn("description", projects[0])

    def test_find_named_item_is_case_insensitive(self):
        item = find_named_item(
            [{"id": "1", "name": "Active Projects"}],
            " active projects ",
            "list",
        )
        self.assertEqual(item["id"], "1")

    def test_formats_multi_option_custom_field(self):
        description = build_custom_fields_description(
            [
                {
                    "id": "labels",
                    "name": "Services",
                    "value": ["a", "b"],
                    "type_config": {
                        "options": [
                            {"id": "a", "label": "Design"},
                            {"id": "b", "label": "Development"},
                        ]
                    },
                }
            ],
            ["labels"],
        )
        self.assertEqual(
            description,
            "ClickUp custom fields:\nServices: Design, Development",
        )

    def test_formats_dropdown_value_by_orderindex(self):
        description = build_custom_fields_description(
            [
                {
                    "id": "stage",
                    "name": "Project Stage",
                    "value": "1",
                    "type_config": {
                        "options": [
                            {"id": "uuid-a", "orderindex": 0, "name": "Planned"},
                            {"id": "uuid-b", "orderindex": 1, "name": "Active"},
                        ]
                    },
                }
            ],
            ["stage"],
        )
        self.assertEqual(
            description,
            "ClickUp custom fields:\nProject Stage: Active",
        )

    def test_removes_decorative_symbols_from_description_field_names(self):
        description = build_custom_fields_description(
            [
                {
                    "id": "project-id",
                    "name": "📌 Project ID",
                    "value": "123",
                },
                {
                    "id": "company-id",
                    "name": "⭐️ Company ID",
                    "value": "456",
                },
            ],
            ["project-id", "company-id"],
        )
        self.assertEqual(
            description,
            "ClickUp custom fields:\nProject ID: 123\nCompany ID: 456",
        )


if __name__ == "__main__":
    unittest.main()

import json
import os
import unittest
from unittest.mock import patch

from fetch_mondaycom import (
    MondayClient,
    MondayFetcher,
    expand_ignored_user_assigned_board_ids,
    load_ignored_user_assigned_board_ids_from_env,
)


def people_column_value(user_id):
    return json.dumps({"personsAndTeams": [{"id": user_id, "kind": "person"}]})


def board_with_item(board_id, item_id, subitem_board_id=None, user_id=9):
    columns = []
    if subitem_board_id is not None:
        columns.append({
            "id": "subtasks",
            "type": "subtasks",
            "settings_str": json.dumps({"boardIds": [subitem_board_id]}),
        })

    return {
        "id": str(board_id),
        "name": f"Board {board_id}",
        "type": "board",
        "groups": [{"id": "group1", "title": "Group", "archived": False}],
        "columns": columns,
        "items_page": {
            "items": [
                {
                    "id": str(item_id),
                    "name": f"Item {item_id}",
                    "state": "active",
                    "group": {"id": "group1"},
                    "parent_item": None,
                    "column_values": [
                        {
                            "id": "person",
                            "type": "people",
                            "value": people_column_value(user_id),
                        }
                    ],
                }
            ]
        },
    }


def subitem_board(board_id, item_id, parent_item_id, user_id=9):
    return {
        "id": str(board_id),
        "name": f"Subitem board {board_id}",
        "type": "sub_items_board",
        "groups": [],
        "columns": [],
        "items_page": {
            "items": [
                {
                    "id": str(item_id),
                    "name": f"Subitem {item_id}",
                    "state": "active",
                    "group": {"id": "group1"},
                    "parent_item": {"id": str(parent_item_id)},
                    "column_values": [
                        {
                            "id": "person",
                            "type": "people",
                            "value": people_column_value(user_id),
                        }
                    ],
                }
            ]
        },
    }


class FakeMondayClient:
    def __init__(self, boards_by_id):
        self.boards_by_id = boards_by_id
        self.get_boards_calls = []

    def get_users(self):
        return [{"id": "9", "email": "ada@example.com", "name": "Ada"}]

    def get_boards(self, board_ids, skip_subscribers_for_board_ids=None):
        self.get_boards_calls.append({
            "board_ids": [str(board_id) for board_id in board_ids],
            "skip_subscribers_for_board_ids": {
                str(board_id)
                for board_id in (skip_subscribers_for_board_ids or [])
            },
        })
        return [
            self.boards_by_id[str(board_id)]
            for board_id in board_ids
            if str(board_id) in self.boards_by_id
        ]


class FetchMondayIgnoredAssignmentsTest(unittest.TestCase):
    def test_load_ignored_board_ids_from_env(self):
        with patch.dict(
            os.environ,
            {"MONDAY_IGNORED_USER_ASSIGNED_BOARD_IDS": " 111, 222 ,"},
            clear=False,
        ):
            self.assertEqual(
                load_ignored_user_assigned_board_ids_from_env(),
                {"111", "222"},
            )

    def test_expand_ignored_board_ids_includes_subitem_boards(self):
        boards = [
            board_with_item("111", "1", subitem_board_id="999"),
            board_with_item("222", "2", subitem_board_id="888"),
        ]

        self.assertEqual(
            expand_ignored_user_assigned_board_ids(["111"], boards),
            {"111", "999"},
        )

    def test_fetch_skips_assigned_users_for_ignored_boards_and_their_subitems(self):
        client = FakeMondayClient({
            "111": board_with_item("111", "1", subitem_board_id="999"),
            "222": board_with_item("222", "2", subitem_board_id="888"),
            "999": subitem_board("999", "11", parent_item_id="1"),
            "888": subitem_board("888", "22", parent_item_id="2"),
        })

        with patch.dict(os.environ, {"MONDAY_API_TOKEN": "token"}, clear=False):
            fetcher = MondayFetcher(
                board_ids=["111", "222"],
                include_done=True,
                ignored_user_assigned_board_ids=["111"],
            )
        fetcher.client = client

        tasks = {task["task_id"]: task for task in fetcher.fetch_all_data()}

        self.assertNotIn("assigned_users", tasks["monday_1"])
        self.assertNotIn("assigned_users", tasks["monday_11"])
        self.assertEqual(
            tasks["monday_2"]["assigned_users"],
            {"9": {"email": "ada@example.com", "username": "Ada"}},
        )
        self.assertEqual(
            tasks["monday_22"]["assigned_users"],
            {"9": {"email": "ada@example.com", "username": "Ada"}},
        )
        self.assertEqual(
            client.get_boards_calls[0]["skip_subscribers_for_board_ids"],
            {"111"},
        )
        self.assertEqual(
            client.get_boards_calls[1]["skip_subscribers_for_board_ids"],
            {"111", "999"},
        )

    def test_get_board_omits_subscribers_when_assignments_are_ignored(self):
        client = MondayClient("token")
        captured = {}

        def fake_request(query, variables=None):
            captured["query"] = query
            captured["variables"] = variables
            return {"boards": [{"id": "111", "items_page": {"items": [], "cursor": None}}]}

        client._request = fake_request
        board = client._get_board("111", include_subscribers=False)

        self.assertEqual(board["id"], "111")
        self.assertNotIn("subscribers", captured["query"])

        client._get_board("111", include_subscribers=True)
        self.assertIn("subscribers", captured["query"])


if __name__ == "__main__":
    unittest.main()

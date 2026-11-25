import json
import os
import unittest
from unittest.mock import patch

from fetch_mondaycom import (
    MondayClient,
    MondayFetcher,
    expand_ignored_user_assigned_board_ids,
    get_done_status_column,
    load_done_status_columns_from_env,
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


SIZE_COLUMN = {
    "id": "color_size",
    "title": "Size",
    "type": "status",
    "settings_str": json.dumps({
        "done_colors": [1],
        "labels": {"0": "Medium", "1": "Small", "2": "Large"},
    }),
}
ITEM_STATUS_COLUMN = {
    "id": "status",
    "title": "Item Status",
    "type": "status",
    "settings_str": json.dumps({
        "done_colors": [1],
        "labels": {"0": "Working on it", "1": "Done", "2": "Stuck", "5": "Not Started"},
    }),
}
TEAM_COLUMN = {
    "id": "color_team",
    "title": "Team",
    "type": "status",
    "settings_str": json.dumps({
        "done_colors": [],
        "labels": {"0": "Design", "1": "Marketing", "2": "Sales"},
    }),
}
PHASE_COLUMN = {
    "id": "color_phase",
    "title": "Phase",
    "type": "status",
    "settings_str": json.dumps({
        "done_colors": [3],
        "labels": {"0": "Planning", "1": "Building", "2": "Testing", "3": "Released"},
    }),
}


def status_column_value(column_id, index):
    value = None if index is None else json.dumps({"index": index})
    return {"id": column_id, "type": "status", "value": value}


def status_item(item_id, statuses, parent_item_id=None):
    return {
        "id": str(item_id),
        "name": f"Item {item_id}",
        "state": "active",
        "group": {"id": "group1"},
        "parent_item": {"id": str(parent_item_id)} if parent_item_id else None,
        "column_values": [
            status_column_value(column_id, index)
            for column_id, index in statuses.items()
        ],
    }


def traffic_studio_boards():
    """Column layout of the Example Board board and its subitem board."""
    main_board = {
        "id": "100",
        "name": "Example Board",
        "type": "board",
        "groups": [{"id": "group1", "title": "Group", "archived": False}],
        "columns": [
            TEAM_COLUMN,
            PHASE_COLUMN,
            {
                "id": "subitems",
                "title": "Subitems",
                "type": "subtasks",
                "settings_str": json.dumps({"boardIds": [200]}),
            },
        ],
        "items_page": {
            "items": [
                status_item("1", {"color_team": 1, "color_phase": 2}),
                status_item("2", {"color_team": 0, "color_phase": 3}),
            ]
        },
    }
    subitems_board = {
        "id": "200",
        "name": "Subitems of Example Board",
        "type": "sub_items_board",
        "groups": [],
        "columns": [SIZE_COLUMN, ITEM_STATUS_COLUMN],
        "items_page": {
            "items": [
                status_item("11", {"color_size": 1, "status": 0}, parent_item_id="1"),
                status_item("12", {"color_size": 1, "status": 5}, parent_item_id="1"),
                status_item("13", {"color_size": 1, "status": 2}, parent_item_id="1"),
                status_item("14", {"color_size": 1, "status": None}, parent_item_id="1"),
                status_item("15", {"color_size": 1, "status": 1}, parent_item_id="1"),
                status_item("16", {"color_size": 0, "status": 1}, parent_item_id="1"),
                status_item("17", {"color_size": 0, "status": 0}, parent_item_id="1"),
            ]
        },
    }
    return {"100": main_board, "200": subitems_board}


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



class FetchMondayDoneStatusTest(unittest.TestCase):
    def fetch_item_ids(self, include_done=False, done_status_columns=()):
        with patch.dict(os.environ, {"MONDAY_API_TOKEN": "token"}, clear=False):
            fetcher = MondayFetcher(
                board_ids=["100"],
                include_done=include_done,
                done_status_columns=done_status_columns,
            )
        fetcher.client = FakeMondayClient(traffic_studio_boards())

        return {
            task["task_id"]
            for task in fetcher.fetch_all_data()
            if task["task_id"] not in {"monday_100", "monday_100_group1"}
        }

    def test_subitems_use_item_status_not_first_status_column(self):
        task_ids = self.fetch_item_ids()

        # A small size with open Item Status stays visible.
        self.assertTrue(
            {"monday_11", "monday_12", "monday_13", "monday_14", "monday_17"} <= task_ids
        )
        # Item Status = Done is hidden, with any size.
        self.assertNotIn("monday_15", task_ids)
        self.assertNotIn("monday_16", task_ids)

    def test_main_board_skips_status_columns_without_done_labels(self):
        task_ids = self.fetch_item_ids()

        self.assertIn("monday_1", task_ids)
        self.assertNotIn("monday_2", task_ids)

    def test_include_done_keeps_done_items(self):
        task_ids = self.fetch_item_ids(include_done=True)

        self.assertTrue({"monday_2", "monday_15", "monday_16"} <= task_ids)

    def test_configured_column_overrides_auto_detection(self):
        task_ids = self.fetch_item_ids(
            done_status_columns=[" size "],
        )

        self.assertEqual(
            task_ids,
            {"monday_1", "monday_16", "monday_17"},
        )

    def test_configured_columns_match_title_or_id_in_order(self):
        board = {
            "columns": [
                SIZE_COLUMN,
                ITEM_STATUS_COLUMN,
                PHASE_COLUMN,
            ],
        }

        self.assertEqual(
            get_done_status_column(board, ["Phase", "status"]).column_id,
            "color_phase",
        )
        self.assertEqual(
            get_done_status_column(board, ["color_size"]).column_id,
            "color_size",
        )

    def test_missing_configured_column_falls_back_to_auto_detection(self):
        board = {"columns": [TEAM_COLUMN, PHASE_COLUMN]}

        done_column = get_done_status_column(board, ["Item Status"])

        self.assertEqual(done_column.column_id, "color_phase")
        self.assertEqual(done_column.done_statuses, frozenset({3}))

    def test_default_status_column_without_done_labels_uses_default_index(self):
        board = {
            "columns": [
                SIZE_COLUMN,
                {"id": "status", "title": "Status", "type": "status", "settings_str": "{}"},
            ],
        }

        done_column = get_done_status_column(board)

        self.assertEqual(done_column.column_id, "status")
        self.assertEqual(done_column.done_statuses, frozenset({1}))

    def test_board_without_done_labels_has_no_done_column(self):
        self.assertIsNone(get_done_status_column({"columns": [TEAM_COLUMN]}))
        self.assertIsNone(get_done_status_column({"columns": []}))

    def test_load_done_status_columns_from_env(self):
        with patch.dict(
            os.environ,
            {"MONDAY_DONE_STATUS_COLUMNS": " Item Status, Phase ,"},
            clear=False,
        ):
            self.assertEqual(
                load_done_status_columns_from_env(),
                ["Item Status", "Phase"],
            )


if __name__ == "__main__":
    unittest.main()

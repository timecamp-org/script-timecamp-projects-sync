import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)


MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2023-10"
MONDAY_DEFAULT_ITEM_LIMIT = 100
MONDAY_DEFAULT_DONE_STATUS_INDEX = 1
MONDAY_FIELD_TYPE_STATUS = "status"
MONDAY_FIELD_TYPE_SUBTASKS = "subtasks"
MONDAY_FIELD_TYPE_PEOPLE = "people"
MONDAY_BOARD_TYPE_SUBITEM_BOARD = "sub_items_board"
MONDAY_INTEGRATION_TASK_PREFIX = "monday_"
MONDAY_MANDATORY_TAGS_OUTPUT_KEY = "meandatory_tags"
MONDAY_ASSIGNED_USERS_OUTPUT_KEY = "assigned_users"


class MondayClient:
    """Client for interacting with the Monday.com GraphQL API."""

    def __init__(self, api_token: str):
        self.headers = {
            "Authorization": api_token,
            "API-Version": MONDAY_API_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "TimeCamp-Monday-Sync",
        }

    def _request(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = requests.post(
            MONDAY_API_URL,
            headers=self.headers,
            json={"query": query, "variables": variables or {}},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("errors"):
            raise ValueError(f"Monday API returned errors: {data['errors']}")

        return data.get("data", {})

    def get_all_boards(self) -> List[Dict[str, Any]]:
        """Get all non-subitem boards from Monday.com."""
        query = """
        query ($page: Int!, $limit: Int!) {
          boards(state: all, page: $page, limit: $limit) {
            id
            name
            state
            type
          }
        }
        """
        boards: List[Dict[str, Any]] = []
        page = 1
        limit = 100

        while True:
            data = self._request(query, {"page": page, "limit": limit})
            page_boards = data.get("boards", [])
            boards.extend(
                board
                for board in page_boards
                if board.get("type") != MONDAY_BOARD_TYPE_SUBITEM_BOARD
            )

            if not page_boards:
                break

            page += 1

        return boards

    def get_users(self) -> List[Dict[str, Any]]:
        """Get Monday.com users for assignment metadata."""
        query = """
        query {
          users(limit: 10000) {
            id
            email
            name
          }
        }
        """
        data = self._request(query)
        return data.get("users", [])

    def get_boards(self, board_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Get boards with groups, columns, and all items."""
        all_boards: List[Dict[str, Any]] = []

        for board_id in board_ids:
            board = self._get_board(board_id)
            if not board:
                continue

            items_page = board.get("items_page") or {}
            all_items = list(items_page.get("items", []))
            cursor = items_page.get("cursor")

            while cursor:
                next_page = self.get_items_by_cursor(cursor)
                all_items.extend(next_page.get("items", []))
                cursor = next_page.get("cursor")

            board["items_page"] = {
                "items": all_items,
                "cursor": None,
            }
            all_boards.append(board)

        return all_boards

    def _get_board(self, board_id: str) -> Optional[Dict[str, Any]]:
        query = """
        query ($boardIds: [ID!], $limit: Int!) {
          boards(ids: $boardIds) {
            id
            name
            state
            type
            subscribers {
              email
            }
            items_page(limit: $limit) {
              cursor
              items {
                id
                name
                state
                column_values {
                  id
                  text
                  value
                  type
                }
                parent_item {
                  id
                }
                group {
                  id
                }
              }
            }
            columns {
              id
              title
              type
              settings_str
            }
            groups {
              id
              title
              archived
            }
          }
        }
        """
        data = self._request(
            query,
            {"boardIds": [str(board_id)], "limit": MONDAY_DEFAULT_ITEM_LIMIT},
        )
        boards = data.get("boards", [])
        return boards[0] if boards else None

    def get_items_by_cursor(self, cursor: str) -> Dict[str, Any]:
        query = """
        query ($cursor: String!, $limit: Int!) {
          next_items_page(limit: $limit, cursor: $cursor) {
            cursor
            items {
              id
              name
              state
              column_values {
                id
                text
                value
                type
              }
              parent_item {
                id
              }
              group {
                id
              }
            }
          }
        }
        """
        data = self._request(
            query,
            {"cursor": cursor, "limit": MONDAY_DEFAULT_ITEM_LIMIT},
        )
        return data.get("next_items_page") or {}


class MondayFetcher:
    """Fetches Monday boards, groups, items, and subitems into tasks.json format."""

    def __init__(
        self,
        board_ids: Optional[List[str]] = None,
        include_done: bool = False,
        include_archived_groups: bool = False,
        mandatory_tag_columns: Optional[List[str]] = None,
    ):
        api_token = os.getenv("MONDAY_API_TOKEN")
        if not api_token:
            raise ValueError("MONDAY_API_TOKEN must be set in .env")

        self.client = MondayClient(api_token)
        self.board_ids = board_ids or load_board_ids_from_env()
        self.include_done = include_done
        self.include_archived_groups = include_archived_groups
        self.mandatory_tag_columns = mandatory_tag_columns or load_mandatory_tag_columns_from_env()
        self.boards_done_statuses: Dict[str, Set[int]] = {}
        self.users_by_id: Dict[str, Dict[str, str]] = {}

    def fetch_all_data(self) -> List[Dict[str, Any]]:
        """Fetch Monday data and return flattened task structure."""
        if not self.board_ids:
            print("Fetching all Monday boards...")
            self.board_ids = [
                board["id"]
                for board in self.client.get_all_boards()
                if board.get("id")
            ]
            print(f"  Found {len(self.board_ids)} non-subitem board(s)")

        print("Fetching selected Monday boards...")
        boards = self.client.get_boards(self.board_ids)
        print(f"  Fetched {len(boards)} board(s)")
        self._fetch_users()

        flattened_data: List[Dict[str, Any]] = []
        subitem_board_ids = self._collect_subitem_board_ids(boards)
        self._fetch_boards_done_statuses(boards)

        for board in boards:
            if board.get("type") == MONDAY_BOARD_TYPE_SUBITEM_BOARD:
                continue

            flattened_data.append({
                "name": board.get("name") or f"Board {board['id']}",
                "task_id": board_external_id(board["id"]),
                "external_task_id": board_external_id(board["id"]),
                "parent_id": 0,
            })

            for group_task in self._iterate_groups(board):
                flattened_data.append(group_task)

            for item_task in self._iterate_items(board):
                flattened_data.append(item_task)

        if subitem_board_ids:
            print(f"Fetching {len(subitem_board_ids)} subitem board(s)...")
            subitem_boards = self.client.get_boards(sorted(subitem_board_ids))
            self._fetch_boards_done_statuses(subitem_boards)

            for board in subitem_boards:
                for subitem_task in self._iterate_items(board):
                    flattened_data.append(subitem_task)

        return flattened_data

    def _iterate_groups(self, board: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        for group in board.get("groups", []):
            if self._is_group_archived(group) and not self.include_archived_groups:
                continue

            yield {
                "name": group.get("title") or f"Group {group['id']}",
                "task_id": group_external_id(board["id"], group["id"]),
                "external_task_id": group_external_id(board["id"], group["id"]),
                "parent_id": board_external_id(board["id"]),
            }

    def _iterate_items(self, board: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        done_statuses = self.boards_done_statuses.get(str(board["id"]), {MONDAY_DEFAULT_DONE_STATUS_INDEX})

        for item in (board.get("items_page") or {}).get("items", []):
            if self._is_subitem_without_parent(board, item):
                continue

            if self._is_item_done(item, done_statuses) and not self.include_done:
                continue

            parent_item = item.get("parent_item")
            if parent_item and parent_item.get("id"):
                parent_id = item_external_id(parent_item["id"])
            else:
                group = item.get("group") or {}
                parent_id = group_external_id(board["id"], group["id"]) if group.get("id") else board_external_id(board["id"])

            task = {
                "name": item.get("name") or f"Item {item['id']}",
                "task_id": item_external_id(item["id"]),
                "external_task_id": item_external_id(item["id"]),
                "parent_id": parent_id,
            }

            mandatory_tags = self._get_mandatory_tags(board, item)
            if mandatory_tags:
                task[MONDAY_MANDATORY_TAGS_OUTPUT_KEY] = mandatory_tags

            assigned_users = self._get_assigned_users(item)
            if assigned_users:
                task[MONDAY_ASSIGNED_USERS_OUTPUT_KEY] = assigned_users

            yield task

    def _collect_subitem_board_ids(self, boards: List[Dict[str, Any]]) -> Set[str]:
        subitem_board_ids: Set[str] = set()

        for board in boards:
            if board.get("type") == MONDAY_BOARD_TYPE_SUBITEM_BOARD:
                continue

            settings = get_settings(
                board.get("columns", []),
                MONDAY_FIELD_TYPE_SUBTASKS,
                "settings_str",
            )
            for board_id in settings.get("boardIds", []):
                subitem_board_ids.add(str(board_id))

        return subitem_board_ids

    def _fetch_boards_done_statuses(self, boards: List[Dict[str, Any]]) -> None:
        for board in boards:
            board_id = str(board["id"])
            self.boards_done_statuses[board_id] = get_done_statuses_for_board(board)

    def _fetch_users(self) -> None:
        try:
            users = self.client.get_users()
        except Exception as exc:
            print(f"Warning: Unable to fetch Monday users for assigned_users: {exc}")
            self.users_by_id = {}
            return

        self.users_by_id = {}
        for user in users:
            user_id = user.get("id")
            if not user_id:
                continue

            user_data = {}
            if user.get("email"):
                user_data["email"] = user["email"]
            if user.get("name"):
                user_data["username"] = user["name"]

            self.users_by_id[str(user_id)] = user_data

        print(f"  Fetched {len(self.users_by_id)} Monday user(s)")

    def _is_group_archived(self, group: Dict[str, Any]) -> bool:
        return bool(group.get("archived"))

    def _is_subitem_without_parent(self, board: Dict[str, Any], item: Dict[str, Any]) -> bool:
        return (
            board.get("type") == MONDAY_BOARD_TYPE_SUBITEM_BOARD
            and not item.get("parent_item")
        )

    def _is_item_done(self, item: Dict[str, Any], done_statuses: Set[int]) -> bool:
        value = get_settings(
            item.get("column_values", []),
            MONDAY_FIELD_TYPE_STATUS,
            "value",
        )
        status_index = value.get("index")

        if status_index is None:
            return False

        return int(status_index) in done_statuses

    def _get_mandatory_tags(self, board: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, List[str]]:
        if not self.mandatory_tag_columns:
            return {}

        column_titles = {
            str(column.get("id")): str(column.get("title") or "")
            for column in board.get("columns", [])
        }
        requested_columns = {
            column_name.lower(): column_name
            for column_name in self.mandatory_tag_columns
        }
        tags: Dict[str, List[str]] = {}

        for column_value in item.get("column_values", []):
            column_title = column_titles.get(str(column_value.get("id")), "")
            output_name = requested_columns.get(column_title.lower())
            if not output_name:
                continue

            values = parse_tag_values(column_value.get("text"))
            if values:
                tags[output_name] = values

        return tags

    def _get_assigned_users(self, item: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
        assigned_users: Dict[str, Dict[str, str]] = {}

        for column_value in item.get("column_values", []):
            if column_value.get("type") != MONDAY_FIELD_TYPE_PEOPLE:
                continue

            for user_id in extract_people_user_ids(column_value.get("value")):
                assigned_users[user_id] = dict(self.users_by_id.get(user_id, {}))

        return assigned_users

    def save_to_json(self, data: List[Dict[str, Any]], filename: str = "tasks.json") -> str:
        """Save data to JSON file."""
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return filename


def get_done_statuses_for_board(board: Dict[str, Any]) -> Set[int]:
    settings = get_settings(
        board.get("columns", []),
        MONDAY_FIELD_TYPE_STATUS,
        "settings_str",
    )
    done_colors = settings.get("done_colors")
    if done_colors is None:
        done_colors = [MONDAY_DEFAULT_DONE_STATUS_INDEX]

    return {int(status_index) for status_index in done_colors}


def get_settings(items: List[Dict[str, Any]], field_type: str, value_key: str) -> Dict[str, Any]:
    for item in items:
        if item.get("type") != field_type:
            continue

        raw_settings = item.get(value_key)
        if not raw_settings:
            return {}

        try:
            decoded = json.loads(raw_settings)
        except (TypeError, json.JSONDecodeError):
            return {}

        return decoded if isinstance(decoded, dict) else {}

    return {}


def board_external_id(board_id: str) -> str:
    return f"{MONDAY_INTEGRATION_TASK_PREFIX}{board_id}"


def group_external_id(board_id: str, group_id: str) -> str:
    return f"{MONDAY_INTEGRATION_TASK_PREFIX}{board_id}_{group_id}"


def item_external_id(item_id: str) -> str:
    return f"{MONDAY_INTEGRATION_TASK_PREFIX}{item_id}"


def parse_tag_values(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return []

    return [
        value.strip()
        for value in raw_value.split(",")
        if value.strip()
    ]


def extract_people_user_ids(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return []

    try:
        decoded = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []

    if not isinstance(decoded, dict):
        return []

    users = []
    for assignment in decoded.get("personsAndTeams", []):
        if not isinstance(assignment, dict):
            continue
        if assignment.get("kind") != "person":
            continue
        if assignment.get("id") is None:
            continue
        users.append(str(assignment["id"]))

    return users


def load_board_ids_from_env() -> List[str]:
    board_ids = os.getenv("MONDAY_BOARD_IDS", "")
    return [
        board_id.strip()
        for board_id in board_ids.split(",")
        if board_id.strip()
    ]


def load_mandatory_tag_columns_from_env() -> List[str]:
    columns = os.getenv("MONDAY_MEANDATORY_TAGS") or os.getenv("MONDAY_MANDATORY_TAGS", "")
    return [
        column.strip()
        for column in columns.split(",")
        if column.strip()
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Monday.com boards, groups, items, and subitems into tasks.json "
            "format for TimeCamp sync."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        default="tasks.json",
        help="Output file path (default: tasks.json)",
    )
    parser.add_argument(
        "--board-ids",
        help="Comma-separated Monday board IDs. Overrides MONDAY_BOARD_IDS.",
    )
    parser.add_argument(
        "--include-done",
        action="store_true",
        help="Include items whose status column is configured as done on the board",
    )
    parser.add_argument(
        "--include-archived-groups",
        action="store_true",
        help="Include archived groups",
    )
    parser.add_argument(
        "--mandatory-tags",
        help=(
            "Comma-separated Monday column titles to add as meandatory_tags. "
            "Overrides MONDAY_MEANDATORY_TAGS/MONDAY_MANDATORY_TAGS."
        ),
    )

    return parser.parse_args()


def main():
    """Fetch Monday.com boards, groups, items, and subitems -> tasks.json."""
    args = parse_args()
    board_ids = None

    if args.board_ids:
        board_ids = [
            board_id.strip()
            for board_id in args.board_ids.split(",")
            if board_id.strip()
        ]

    mandatory_tag_columns = None
    if args.mandatory_tags:
        mandatory_tag_columns = [
            column.strip()
            for column in args.mandatory_tags.split(",")
            if column.strip()
        ]

    print("Starting Monday.com data fetch...")
    print(f"Started at: {datetime.now()}")

    fetcher = MondayFetcher(
        board_ids=board_ids,
        include_done=args.include_done,
        include_archived_groups=args.include_archived_groups,
        mandatory_tag_columns=mandatory_tag_columns,
    )
    data = fetcher.fetch_all_data()
    filename = fetcher.save_to_json(data, args.output)

    boards = len([item for item in data if item["parent_id"] == 0])
    groups = len([
        item
        for item in data
        if "_" in str(item["task_id"]).removeprefix("monday_")
    ])
    items = len(data) - boards - groups

    print(f"\nData fetch completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")
    print("\nSummary:")
    print(f"  Boards: {boards}")
    print(f"  Groups: {groups}")
    print(f"  Items/subitems: {items}")
    print(f"  Total items: {len(data)}")

    if data:
        print("\nStructure preview:")
        for item in data[:20]:
            task_id = str(item["task_id"])
            parent_id = item["parent_id"]

            if parent_id == 0:
                indent = ""
                level = "[BOARD]"
            elif "_" in task_id.removeprefix("monday_"):
                indent = "  "
                level = "[GROUP]"
            else:
                indent = "    "
                level = "[ITEM]"

            print(
                f"  {indent}{level} {item['name']} "
                f"(ID: {item['task_id']}, Parent: {parent_id})"
            )

        if len(data) > 20:
            print(f"  ... and {len(data) - 20} more items")


if __name__ == "__main__":
    main()

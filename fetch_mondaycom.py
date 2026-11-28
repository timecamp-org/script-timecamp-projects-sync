import argparse
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)


MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2023-10"
MONDAY_DEFAULT_ITEM_LIMIT = 100
MONDAY_DEFAULT_DONE_STATUS_INDEX = 1
MONDAY_DEFAULT_STATUS_COLUMN_ID = "status"
MONDAY_FIELD_TYPE_STATUS = "status"
MONDAY_FIELD_TYPE_SUBTASKS = "subtasks"
MONDAY_FIELD_TYPE_PEOPLE = "people"
MONDAY_BOARD_TYPE_SUBITEM_BOARD = "sub_items_board"
MONDAY_INTEGRATION_TASK_PREFIX = "monday_"
MONDAY_MANDATORY_TAGS_OUTPUT_KEY = "meandatory_tags"
MONDAY_ASSIGNED_USERS_OUTPUT_KEY = "assigned_users"


@dataclass(frozen=True)
class DoneStatusColumn:
    """Status column that tells if an item on a board is done."""

    column_id: str
    title: str
    done_statuses: FrozenSet[int]


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

    def get_board_columns(self, board_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Get Monday board column metadata without fetching board items."""
        query = """
        query ($boardIds: [ID!]) {
          boards(ids: $boardIds) {
            id
            name
            type
            columns {
              id
              title
              type
              settings_str
            }
          }
        }
        """
        normalized_board_ids = [str(board_id) for board_id in board_ids]
        if not normalized_board_ids:
            return []

        data = self._request(query, {"boardIds": normalized_board_ids})
        return data.get("boards", [])

    def get_items_by_ids(self, item_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Get Monday items by id with board and column value metadata."""
        query = """
        query ($itemIds: [ID!], $limit: Int!) {
          items(ids: $itemIds, limit: $limit) {
            id
            name
            state
            board {
              id
              name
              type
            }
            parent_item {
              id
            }
            column_values {
              id
              text
              value
              type
            }
          }
        }
        """
        normalized_item_ids = [str(item_id) for item_id in item_ids]
        if not normalized_item_ids:
            return []

        items: List[Dict[str, Any]] = []
        batch_size = 100
        for index in range(0, len(normalized_item_ids), batch_size):
            batch = normalized_item_ids[index:index + batch_size]
            data = self._request(query, {"itemIds": batch, "limit": len(batch)})
            items.extend(data.get("items", []))

        return items

    def change_simple_column_value(
        self,
        board_id: str,
        item_id: str,
        column_id: str,
        value: str,
    ) -> Dict[str, Any]:
        """Set a Monday simple column value, e.g. a numbers column."""
        mutation = """
        mutation (
          $boardId: ID!,
          $itemId: ID!,
          $columnId: String!,
          $value: String!
        ) {
          change_simple_column_value(
            board_id: $boardId,
            item_id: $itemId,
            column_id: $columnId,
            value: $value
          ) {
            id
          }
        }
        """
        data = self._request(
            mutation,
            {
                "boardId": str(board_id),
                "itemId": str(item_id),
                "columnId": str(column_id),
                "value": str(value),
            },
        )
        return data.get("change_simple_column_value") or {}

    def get_boards(
        self,
        board_ids: Iterable[str],
        skip_subscribers_for_board_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get boards with groups, columns, and all items."""
        all_boards: List[Dict[str, Any]] = []
        skip_subscribers = {
            str(board_id)
            for board_id in (skip_subscribers_for_board_ids or [])
        }

        for board_id in board_ids:
            board = self._get_board(
                board_id,
                include_subscribers=str(board_id) not in skip_subscribers,
            )
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

    def _get_board(
        self,
        board_id: str,
        include_subscribers: bool = True,
    ) -> Optional[Dict[str, Any]]:
        subscribers_fields = """
            subscribers {
              email
            }
        """ if include_subscribers else ""
        query = f"""
        query ($boardIds: [ID!], $limit: Int!) {{
          boards(ids: $boardIds) {{
            id
            name
            state
            type
            {subscribers_fields}
            items_page(limit: $limit) {{
              cursor
              items {{
                id
                name
                state
                column_values {{
                  id
                  text
                  value
                  type
                }}
                parent_item {{
                  id
                }}
                group {{
                  id
                }}
              }}
            }}
            columns {{
              id
              title
              type
              settings_str
            }}
            groups {{
              id
              title
              archived
            }}
          }}
        }}
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
        ignored_user_assigned_board_ids: Optional[Iterable[str]] = None,
        done_status_columns: Optional[Iterable[str]] = None,
        subitem_mandatory_tag_columns: Optional[Dict[str, str]] = None,
        placeholder_tags: Optional[Iterable[str]] = None,
    ):
        api_token = os.getenv("MONDAY_API_TOKEN")
        if not api_token:
            raise ValueError("MONDAY_API_TOKEN must be set in .env")

        self.client = MondayClient(api_token)
        self.board_ids = board_ids or load_board_ids_from_env()
        self.include_done = include_done
        self.include_archived_groups = include_archived_groups
        self.mandatory_tag_columns = mandatory_tag_columns or load_mandatory_tag_columns_from_env()
        self.subitem_mandatory_tag_columns = (
            subitem_mandatory_tag_columns
            if subitem_mandatory_tag_columns is not None
            else load_subitem_mandatory_tag_columns_from_env()
        )
        self.placeholder_tags = list(
            placeholder_tags
            if placeholder_tags is not None
            else load_placeholder_tags_from_env()
        )
        self.subitem_tag_values: Dict[str, Dict[str, List[str]]] = {}
        self.subitem_tag_result = SubitemMandatoryTagResult()
        self.ignored_user_assigned_board_ids = normalize_board_ids(
            ignored_user_assigned_board_ids
            if ignored_user_assigned_board_ids is not None
            else load_ignored_user_assigned_board_ids_from_env()
        )
        self._assignment_ignored_board_ids = set(self.ignored_user_assigned_board_ids)
        self.done_status_columns = list(
            done_status_columns
            if done_status_columns is not None
            else load_done_status_columns_from_env()
        )
        self.boards_done_columns: Dict[str, Optional[DoneStatusColumn]] = {}
        self.users_by_id: Dict[str, Dict[str, str]] = {}

    def fetch_all_data(self) -> List[Dict[str, Any]]:
        """Fetch Monday data and return flattened task structure."""
        task_sets = self.fetch_task_sets()
        return task_sets.with_done if self.include_done else task_sets.active

    def fetch_task_sets(self) -> "MondayTaskSets":
        """Fetch Monday data once and return tasks without and with done items."""
        if not self.board_ids:
            print("Fetching all Monday boards...")
            self.board_ids = [
                board["id"]
                for board in self.client.get_all_boards()
                if board.get("id")
            ]
            print(f"  Found {len(self.board_ids)} non-subitem board(s)")

        print("Fetching selected Monday boards...")
        boards = self.client.get_boards(
            self.board_ids,
            skip_subscribers_for_board_ids=self.ignored_user_assigned_board_ids,
        )
        print(f"  Fetched {len(boards)} board(s)")
        self._fetch_users()

        flattened_data: List[Dict[str, Any]] = []
        done_task_ids: Set[str] = set()
        self.subitem_tag_values = {}
        subitem_board_ids = self._collect_subitem_board_ids(boards)
        self._assignment_ignored_board_ids = expand_ignored_user_assigned_board_ids(
            self.ignored_user_assigned_board_ids,
            boards,
        )
        if self.ignored_user_assigned_board_ids:
            print(
                "  Skipping user assignments for "
                f"{len(self.ignored_user_assigned_board_ids)} configured board(s)"
            )
        self._fetch_boards_done_columns(boards)

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

            for item_task, is_done in self._iterate_items(board):
                flattened_data.append(item_task)
                if is_done:
                    done_task_ids.add(item_task["task_id"])

        if subitem_board_ids:
            print(f"Fetching {len(subitem_board_ids)} subitem board(s)...")
            subitem_boards = self.client.get_boards(
                sorted(subitem_board_ids),
                skip_subscribers_for_board_ids=self._assignment_ignored_board_ids,
            )
            self._fetch_boards_done_columns(subitem_boards)

            for board in subitem_boards:
                for subitem_task, is_done in self._iterate_items(board):
                    flattened_data.append(subitem_task)
                    if is_done:
                        done_task_ids.add(subitem_task["task_id"])

        self.subitem_tag_result = apply_subitem_mandatory_tags(
            flattened_data,
            self.subitem_tag_values,
            self.placeholder_tags,
        )
        if self.subitem_mandatory_tag_columns:
            print(
                "  Subitem mandatory tags: "
                f"{self.subitem_tag_result.assigned} subitem(s) use their own column, "
                f"{self.subitem_tag_result.mapped} of them mapped to another tag value"
            )

        return MondayTaskSets(
            active=[
                task for task in flattened_data
                if task["task_id"] not in done_task_ids
            ],
            with_done=flattened_data,
        )

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

    def _iterate_items(self, board: Dict[str, Any]) -> Iterable[Tuple[Dict[str, Any], bool]]:
        """Yield (task, is_done) for every item on the board."""
        done_column = self.boards_done_columns.get(str(board["id"]))

        for item in (board.get("items_page") or {}).get("items", []):
            if self._is_subitem_without_parent(board, item):
                continue

            is_done = self._is_item_done(item, done_column)

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

            if board.get("type") == MONDAY_BOARD_TYPE_SUBITEM_BOARD:
                subitem_tag_values = self._get_subitem_tag_values(board, item)
                if subitem_tag_values:
                    self.subitem_tag_values[task["task_id"]] = subitem_tag_values

            if self._should_export_assigned_users(board):
                assigned_users = self._get_assigned_users(item)
                if assigned_users:
                    task[MONDAY_ASSIGNED_USERS_OUTPUT_KEY] = assigned_users

            yield task, is_done

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

    def _fetch_boards_done_columns(self, boards: List[Dict[str, Any]]) -> None:
        for board in boards:
            done_column = get_done_status_column(board, self.done_status_columns)
            self.boards_done_columns[str(board["id"])] = done_column

            board_name = board.get("name") or f"Board {board['id']}"
            if done_column:
                print(
                    f"  Done status column for {board_name}: "
                    f"{done_column.title} ({done_column.column_id})"
                )
            else:
                print(f"  No done status column for {board_name}; keeping all items")

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

    def _is_item_done(
        self,
        item: Dict[str, Any],
        done_column: Optional[DoneStatusColumn],
    ) -> bool:
        if done_column is None:
            return False

        for column_value in item.get("column_values", []):
            if str(column_value.get("id")) != done_column.column_id:
                continue

            status_index = parse_json_object(column_value.get("value")).get("index")
            if status_index is None:
                return False

            return int(status_index) in done_column.done_statuses

        return False

    def _get_mandatory_tags(self, board: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, List[str]]:
        if not self.mandatory_tag_columns:
            return {}

        return get_column_tag_values(
            board,
            item,
            {column_name: column_name for column_name in self.mandatory_tag_columns},
        )

    def _get_subitem_tag_values(
        self,
        board: Dict[str, Any],
        item: Dict[str, Any],
    ) -> Dict[str, List[str]]:
        if not self.subitem_mandatory_tag_columns:
            return {}

        return get_column_tag_values(
            board,
            item,
            {
                column_title: tag_list_name
                for tag_list_name, column_title in self.subitem_mandatory_tag_columns.items()
            },
        )

    def _should_export_assigned_users(self, board: Dict[str, Any]) -> bool:
        return str(board.get("id")) not in self._assignment_ignored_board_ids

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


@dataclass
class MondayTaskSets:
    """Tasks from one Monday fetch, without and with items marked as done."""

    active: List[Dict[str, Any]]
    with_done: List[Dict[str, Any]]


@dataclass
class SubitemMandatoryTagResult:
    assigned: int = 0
    mapped: int = 0


def get_column_tag_values(
    board: Dict[str, Any],
    item: Dict[str, Any],
    output_names_by_column_title: Dict[str, str],
) -> Dict[str, List[str]]:
    """Read comma-separated column values, keyed by the requested output name."""
    column_titles = {
        str(column.get("id")): str(column.get("title") or "")
        for column in board.get("columns", [])
    }
    requested_columns = {
        column_title.lower(): output_name
        for column_title, output_name in output_names_by_column_title.items()
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


def apply_subitem_mandatory_tags(
    tasks: List[Dict[str, Any]],
    subitem_tag_values: Dict[str, Dict[str, List[str]]],
    placeholder_tags: Iterable[str],
) -> SubitemMandatoryTagResult:
    """Give subitems their own mandatory tags when the inherited tag is missing
    or only a placeholder value.

    A subitem column value is mapped to the tag value that the same column value
    has most often under rows with a real (non-placeholder) tag. Without such
    rows the column value itself is used.
    """
    result = SubitemMandatoryTagResult()
    if not subitem_tag_values:
        return result

    placeholder_keys = {normalize_tag_key(tag) for tag in placeholder_tags}
    tasks_by_id = {str(task["task_id"]): task for task in tasks}
    value_map = build_subitem_tag_value_map(
        tasks_by_id,
        subitem_tag_values,
        placeholder_keys,
    )

    for task_id, values_by_tag_list in subitem_tag_values.items():
        task = tasks_by_id.get(task_id)
        if not task:
            continue

        own_tags = task.get(MONDAY_MANDATORY_TAGS_OUTPUT_KEY) or {}
        inherited_tags = get_inherited_mandatory_tags(task, tasks_by_id)

        for tag_list_name, values in values_by_tag_list.items():
            if own_tags.get(tag_list_name):
                continue
            if not is_missing_or_placeholder(
                inherited_tags.get(tag_list_name),
                placeholder_keys,
            ):
                continue

            source_values = [
                value for value in values
                if normalize_tag_key(value) not in placeholder_keys
            ]
            if not source_values:
                continue

            tag_list_map = value_map.get(tag_list_name, {})
            tag_values = unique_tag_values(
                tag_list_map.get(normalize_tag_key(value), value)
                for value in source_values
            )
            task.setdefault(MONDAY_MANDATORY_TAGS_OUTPUT_KEY, {})[tag_list_name] = tag_values
            result.assigned += 1
            if [normalize_tag_key(value) for value in tag_values] != [
                normalize_tag_key(value) for value in source_values
            ]:
                result.mapped += 1

    return result


def build_subitem_tag_value_map(
    tasks_by_id: Dict[str, Dict[str, Any]],
    subitem_tag_values: Dict[str, Dict[str, List[str]]],
    placeholder_keys: Set[str],
) -> Dict[str, Dict[str, str]]:
    """Map each subitem column value to its most common real tag value."""
    counts: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))

    for task_id, values_by_tag_list in subitem_tag_values.items():
        task = tasks_by_id.get(task_id)
        if not task:
            continue

        own_tags = task.get(MONDAY_MANDATORY_TAGS_OUTPUT_KEY) or {}
        inherited_tags = get_inherited_mandatory_tags(task, tasks_by_id)

        for tag_list_name, values in values_by_tag_list.items():
            target_values = own_tags.get(tag_list_name) or inherited_tags.get(tag_list_name) or []
            if len(target_values) != 1:
                continue

            target_value = target_values[0]
            if normalize_tag_key(target_value) in placeholder_keys:
                continue

            for value in values:
                value_key = normalize_tag_key(value)
                if value_key in placeholder_keys:
                    continue
                counts[tag_list_name][value_key][target_value] += 1

    return {
        tag_list_name: {
            value_key: min(
                target_counts.items(),
                key=lambda target: (-target[1], target[0].lower(), target[0]),
            )[0]
            for value_key, target_counts in values.items()
        }
        for tag_list_name, values in counts.items()
    }


def get_inherited_mandatory_tags(
    task: Dict[str, Any],
    tasks_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Return mandatory tags from the nearest ancestor that sets each tag list."""
    inherited_tags: Dict[str, List[str]] = {}
    visited: Set[str] = set()
    parent_id = task.get("parent_id")

    while parent_id not in (None, "", 0, "0") and str(parent_id) not in visited:
        visited.add(str(parent_id))
        parent_task = tasks_by_id.get(str(parent_id))
        if not parent_task:
            break

        for tag_list_name, values in (parent_task.get(MONDAY_MANDATORY_TAGS_OUTPUT_KEY) or {}).items():
            if values:
                inherited_tags.setdefault(tag_list_name, values)
        parent_id = parent_task.get("parent_id")

    return inherited_tags


def is_missing_or_placeholder(values: Optional[List[str]], placeholder_keys: Set[str]) -> bool:
    if not values:
        return True

    return all(normalize_tag_key(value) in placeholder_keys for value in values)


def normalize_tag_key(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


def unique_tag_values(values: Iterable[str]) -> List[str]:
    unique_values: List[str] = []
    seen: Set[str] = set()

    for value in values:
        value_key = normalize_tag_key(value)
        if value_key and value_key not in seen:
            unique_values.append(value)
            seen.add(value_key)

    return unique_values


def get_done_status_column(
    board: Dict[str, Any],
    configured_columns: Optional[Iterable[str]] = None,
) -> Optional[DoneStatusColumn]:
    """Return the status column that tells if an item on the board is done.

    A board can have more status columns (e.g. "Size") before the real
    item status, so the first status column is not always correct. Priority:
    configured columns (title or id), the default Monday column with id
    "status", then the first status column with done labels.
    """
    status_columns = [
        column
        for column in board.get("columns", [])
        if column.get("type") == MONDAY_FIELD_TYPE_STATUS
    ]

    for configured_column in configured_columns or []:
        for column in status_columns:
            if is_column_match(column, configured_column):
                return create_done_status_column(column)

    for column in status_columns:
        if str(column.get("id")) == MONDAY_DEFAULT_STATUS_COLUMN_ID:
            return create_done_status_column(column)

    for column in status_columns:
        if get_column_done_colors(column):
            return create_done_status_column(column)

    return None


def create_done_status_column(column: Dict[str, Any]) -> DoneStatusColumn:
    done_colors = get_column_done_colors(column)
    if done_colors is None:
        done_colors = [MONDAY_DEFAULT_DONE_STATUS_INDEX]

    return DoneStatusColumn(
        column_id=str(column.get("id")),
        title=str(column.get("title") or column.get("id")),
        done_statuses=frozenset(int(status_index) for status_index in done_colors),
    )


def get_column_done_colors(column: Dict[str, Any]) -> Optional[List[Any]]:
    done_colors = parse_json_object(column.get("settings_str")).get("done_colors")
    return done_colors if isinstance(done_colors, list) else None


def is_column_match(column: Dict[str, Any], column_name: str) -> bool:
    normalized_name = column_name.strip().lower()
    return normalized_name in {
        str(column.get("id") or "").strip().lower(),
        str(column.get("title") or "").strip().lower(),
    }


def get_settings(items: List[Dict[str, Any]], field_type: str, value_key: str) -> Dict[str, Any]:
    for item in items:
        if item.get("type") == field_type:
            return parse_json_object(item.get(value_key))

    return {}


def parse_json_object(raw_value: Optional[str]) -> Dict[str, Any]:
    if not raw_value:
        return {}

    try:
        decoded = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return {}

    return decoded if isinstance(decoded, dict) else {}


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


def normalize_board_ids(board_ids: Optional[Iterable[str]]) -> Set[str]:
    return {
        str(board_id).strip()
        for board_id in (board_ids or [])
        if str(board_id).strip()
    }


def parse_board_ids(raw_value: Optional[str]) -> Set[str]:
    return normalize_board_ids((raw_value or "").split(","))


def load_board_ids_from_env() -> List[str]:
    board_ids = os.getenv("MONDAY_BOARD_IDS", "")
    return [
        board_id.strip()
        for board_id in board_ids.split(",")
        if board_id.strip()
    ]


def load_ignored_user_assigned_board_ids_from_env() -> Set[str]:
    return parse_board_ids(os.getenv("MONDAY_IGNORED_USER_ASSIGNED_BOARD_IDS", ""))


def expand_ignored_user_assigned_board_ids(
    ignored_board_ids: Iterable[str],
    boards: List[Dict[str, Any]],
) -> Set[str]:
    """Include subitem boards that belong to ignored parent boards."""
    ignored = normalize_board_ids(ignored_board_ids)

    for board in boards:
        if str(board.get("id")) not in ignored:
            continue

        settings = get_settings(
            board.get("columns", []),
            MONDAY_FIELD_TYPE_SUBTASKS,
            "settings_str",
        )
        for board_id in settings.get("boardIds", []):
            ignored.add(str(board_id))

    return ignored


def load_mandatory_tag_columns_from_env() -> List[str]:
    columns = os.getenv("MONDAY_MEANDATORY_TAGS") or os.getenv("MONDAY_MANDATORY_TAGS", "")
    return [
        column.strip()
        for column in columns.split(",")
        if column.strip()
    ]


def load_done_status_columns_from_env() -> List[str]:
    columns = os.getenv("MONDAY_DONE_STATUS_COLUMNS", "")
    return [
        column.strip()
        for column in columns.split(",")
        if column.strip()
    ]


def parse_subitem_mandatory_tag_columns(raw_value: Optional[str]) -> Dict[str, str]:
    """Parse "Tag list=Subitem column title" pairs, separated by commas."""
    columns: Dict[str, str] = {}

    for raw_pair in (raw_value or "").split(","):
        pair = raw_pair.strip()
        if not pair:
            continue

        tag_list_name, separator, column_title = pair.partition("=")
        if not separator or not tag_list_name.strip() or not column_title.strip():
            raise ValueError(
                f"Invalid subitem mandatory tag column {pair!r}; "
                "expected 'Tag list=Subitem column title'"
            )
        columns[tag_list_name.strip()] = column_title.strip()

    return columns


def load_subitem_mandatory_tag_columns_from_env() -> Dict[str, str]:
    return parse_subitem_mandatory_tag_columns(
        os.getenv("MONDAY_SUBITEM_MEANDATORY_TAGS")
        or os.getenv("MONDAY_SUBITEM_MANDATORY_TAGS", "")
    )


def load_placeholder_tags_from_env() -> List[str]:
    return parse_tag_values(os.getenv("MONDAY_PLACEHOLDER_TAGS", ""))


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
    parser.add_argument(
        "--subitem-mandatory-tags",
        help=(
            "Comma-separated 'Tag list=Subitem column title' pairs. A subitem uses "
            "its own column when the tag it inherits is missing or a placeholder. "
            "Overrides MONDAY_SUBITEM_MEANDATORY_TAGS/MONDAY_SUBITEM_MANDATORY_TAGS."
        ),
    )
    parser.add_argument(
        "--placeholder-tags",
        help=(
            "Comma-separated tag values that do not name one real value, so "
            "subitems replace them with their own column. "
            "Overrides MONDAY_PLACEHOLDER_TAGS."
        ),
    )
    parser.add_argument(
        "--output-with-done",
        help=(
            "Also write tasks including done items to this file, from the same "
            "fetch. Use it for exports and time entry tags of finished work."
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

    subitem_mandatory_tag_columns = None
    if args.subitem_mandatory_tags:
        subitem_mandatory_tag_columns = parse_subitem_mandatory_tag_columns(
            args.subitem_mandatory_tags
        )

    placeholder_tags = None
    if args.placeholder_tags is not None:
        placeholder_tags = parse_tag_values(args.placeholder_tags)

    print("Starting Monday.com data fetch...")
    print(f"Started at: {datetime.now()}")

    fetcher = MondayFetcher(
        board_ids=board_ids,
        include_done=args.include_done,
        include_archived_groups=args.include_archived_groups,
        mandatory_tag_columns=mandatory_tag_columns,
        subitem_mandatory_tag_columns=subitem_mandatory_tag_columns,
        placeholder_tags=placeholder_tags,
    )
    task_sets = fetcher.fetch_task_sets()
    data = task_sets.with_done if args.include_done else task_sets.active
    filename = fetcher.save_to_json(data, args.output)
    if args.output_with_done:
        with_done_filename = fetcher.save_to_json(task_sets.with_done, args.output_with_done)
        print(
            f"Tasks including done items saved to: {with_done_filename} "
            f"({len(task_sets.with_done)} tasks)"
        )

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

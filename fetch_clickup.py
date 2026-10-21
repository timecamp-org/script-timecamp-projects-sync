import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv


load_dotenv(override=True)


CLICKUP_API_BASE_URL = "https://api.clickup.com/api/v2"
DEFAULT_WORKSPACE_NAME = "Workspace"
DEFAULT_SPACE_NAME = "Space"
DEFAULT_FOLDER_NAME = "Folder"
DEFAULT_LIST_NAME = "List"


class ClickUpClient:
    """Small read-only client for the ClickUp API v2."""

    def __init__(self, oauth_access_token: str):
        self.base_url = CLICKUP_API_BASE_URL
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {oauth_access_token}",
                "User-Agent": "TimeCamp-ClickUp-Sync",
            }
        )

    def _get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(
                f"Unexpected ClickUp response for {endpoint}: {type(data)}"
            )
        return data

    def get_workspaces(self) -> List[Dict[str, Any]]:
        return self._get("team").get("teams", [])

    def get_spaces(self, workspace_id: str) -> List[Dict[str, Any]]:
        return self._get(
            f"team/{workspace_id}/space",
            {"archived": "false"},
        ).get("spaces", [])

    def get_folders(self, space_id: str) -> List[Dict[str, Any]]:
        return self._get(
            f"space/{space_id}/folder",
            {"archived": "false"},
        ).get("folders", [])

    def get_lists(self, folder_id: str) -> List[Dict[str, Any]]:
        return self._get(
            f"folder/{folder_id}/list",
            {"archived": "false"},
        ).get("lists", [])

    def get_tasks(
        self,
        list_id: str,
        include_closed: bool = False,
    ) -> List[Dict[str, Any]]:
        tasks: List[Dict[str, Any]] = []
        page = 0

        while True:
            page_tasks = self._get(
                f"list/{list_id}/task",
                {
                    "archived": "false",
                    "include_closed": str(include_closed).lower(),
                    "subtasks": "false",
                    "page": page,
                },
            ).get("tasks", [])
            tasks.extend(page_tasks)

            # ClickUp returns at most 100 tasks per page.
            if len(page_tasks) < 100:
                break
            page += 1

        return tasks


class ClickUpFetcher:
    """Fetch one ClickUp List as a flat set of TimeCamp projects."""

    def __init__(self, client: Optional[ClickUpClient] = None):
        access_token = os.getenv("CLICKUP_OAUTH_ACCESS_TOKEN")
        if client is None and not access_token:
            raise ValueError("CLICKUP_OAUTH_ACCESS_TOKEN must be set in .env")

        self.client = client or ClickUpClient(access_token or "")
        self.workspace_name = os.getenv(
            "CLICKUP_WORKSPACE_NAME", DEFAULT_WORKSPACE_NAME
        ).strip()
        if not self.workspace_name:
            raise ValueError("CLICKUP_WORKSPACE_NAME must be set in .env")
        self.space_name = os.getenv("CLICKUP_SPACE_NAME", DEFAULT_SPACE_NAME)
        self.folder_name = os.getenv("CLICKUP_FOLDER_NAME", DEFAULT_FOLDER_NAME)
        self.list_name = os.getenv("CLICKUP_LIST_NAME", DEFAULT_LIST_NAME)
        self.include_closed = env_bool("CLICKUP_INCLUDE_CLOSED", False)
        self.description_field_selectors = [
            value.strip()
            for value in os.getenv(
                "CLICKUP_DESCRIPTION_CUSTOM_FIELDS", ""
            ).split(",")
            if value.strip()
        ]

    def fetch_all_data(self) -> List[Dict[str, Any]]:
        workspace = find_named_item(
            self.client.get_workspaces(),
            self.workspace_name,
            "workspace",
        )
        space = find_named_item(
            self.client.get_spaces(str(workspace["id"])),
            self.space_name,
            "space",
        )
        folder = find_named_item(
            self.client.get_folders(str(space["id"])),
            self.folder_name,
            "folder",
        )
        clickup_list = find_named_item(
            self.client.get_lists(str(folder["id"])),
            self.list_name,
            "list",
        )

        print(
            "Fetching ClickUp projects from "
            f"{workspace['name']} / {space['name']} / "
            f"{folder['name']} / {clickup_list['name']}..."
        )
        raw_tasks = self.client.get_tasks(
            str(clickup_list["id"]),
            include_closed=self.include_closed,
        )

        projects = [
            self._to_timecamp_project(task)
            for task in raw_tasks
            # Defensive filter: the API should already omit subtasks.
            if not task.get("parent")
        ]
        print(f"  Found {len(projects)} project(s); subitems were omitted")
        return projects

    def _to_timecamp_project(self, task: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(task["id"])
        project: Dict[str, Any] = {
            "name": task.get("name") or f"ClickUp project {task_id}",
            "task_id": f"clickup_{task_id}",
            "external_task_id": f"clickup_{task_id}",
            "parent_id": 0,
        }

        assigned_users = build_assigned_users(task.get("assignees", []))
        if assigned_users:
            project["assigned_users"] = assigned_users

        description = build_custom_fields_description(
            task.get("custom_fields", []),
            self.description_field_selectors,
        )
        if description:
            project["description"] = description

        return project

    @staticmethod
    def save_to_json(
        data: List[Dict[str, Any]],
        filename: str = "tasks.json",
    ) -> str:
        with open(filename, "w", encoding="utf-8") as output_file:
            json.dump(data, output_file, indent=2, ensure_ascii=False)
        return filename


def find_named_item(
    items: Iterable[Dict[str, Any]],
    requested_name: str,
    item_type: str,
) -> Dict[str, Any]:
    matches = [
        item
        for item in items
        if str(item.get("name", "")).strip().casefold()
        == requested_name.strip().casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"More than one ClickUp {item_type} is named {requested_name!r}"
        )

    available = ", ".join(
        sorted(str(item.get("name", "")) for item in items if item.get("name"))
    )
    raise ValueError(
        f"ClickUp {item_type} {requested_name!r} was not found. "
        f"Available: {available or '(none)'}"
    )


def build_assigned_users(
    assignees: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for assignee in assignees:
        user_id = assignee.get("id")
        if user_id is None:
            continue

        user: Dict[str, str] = {}
        if assignee.get("email"):
            user["email"] = str(assignee["email"])
        if assignee.get("username"):
            user["username"] = str(assignee["username"])
        result[str(user_id)] = user
    return result


def build_custom_fields_description(
    custom_fields: Iterable[Dict[str, Any]],
    selectors: Iterable[str],
) -> str:
    fields = list(custom_fields)
    lines: List[str] = []

    for selector in selectors:
        field = find_custom_field(fields, selector)
        if field is None:
            print(
                f"Warning: ClickUp custom field {selector!r} was not found "
                "on a project"
            )
            continue

        value = format_custom_field_value(field)
        if value:
            field_name = custom_field_display_name(
                str(field.get("name") or selector)
            )
            lines.append(f"{field_name}: {value}")

    if not lines:
        return ""
    return "ClickUp custom fields:\n" + "\n".join(lines)


def custom_field_display_name(name: str) -> str:
    """Remove decorative symbols used by selected ClickUp field names."""
    return name.replace("🆔", "").replace("🔢", "").strip()


def find_custom_field(
    custom_fields: Iterable[Dict[str, Any]],
    selector: str,
) -> Optional[Dict[str, Any]]:
    normalized_selector = selector.strip().casefold()

    # ID is preferred, so a field name cannot accidentally shadow it.
    for field in custom_fields:
        if str(field.get("id", "")).strip().casefold() == normalized_selector:
            return field
    for field in custom_fields:
        if str(field.get("name", "")).strip().casefold() == normalized_selector:
            return field
    return None


def format_custom_field_value(field: Dict[str, Any]) -> str:
    value = field.get("value")
    if value is None:
        return ""

    options: Dict[str, str] = {}
    for option in (field.get("type_config") or {}).get("options", []):
        label = option.get("name") or option.get("label") or option.get("id")
        if label is None:
            continue
        if option.get("id") is not None:
            options[str(option["id"])] = str(label)
        # ClickUp dropdown fields use orderindex as their stored value.
        if option.get("orderindex") is not None:
            options[str(option["orderindex"])] = str(label)

    if isinstance(value, list):
        formatted_values = [
            format_complex_value(item, options)
            for item in value
        ]
        return ", ".join(item for item in formatted_values if item)
    if isinstance(value, dict):
        return format_complex_value(value, options)

    return options.get(str(value), str(value))


def format_complex_value(value: Any, options: Dict[str, str]) -> str:
    if isinstance(value, dict):
        for key in ("username", "email", "name", "label"):
            if value.get(key):
                return str(value[key])
        if value.get("id") is not None:
            return options.get(str(value["id"]), str(value["id"]))
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return options.get(str(value), str(value))


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a flat project list from ClickUp into tasks.json for "
            "TimeCamp synchronization."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        default="tasks.json",
        help="Output file path (default: tasks.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("Starting ClickUp project fetch...")
    print(f"Started at: {datetime.now()}")

    fetcher = ClickUpFetcher()
    projects = fetcher.fetch_all_data()
    filename = fetcher.save_to_json(projects, args.output)

    print(f"\nData fetch completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")
    print(f"Projects: {len(projects)}")


if __name__ == "__main__":
    main()

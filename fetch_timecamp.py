import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

from dotenv import load_dotenv

from src.timecamp_client import TimeCampClient


SOURCE_PREFIX = "timecamp"
DEFAULT_OUTPUT_FILE = "tasks.json"


def prefixed_task_id(task_id: Any) -> str:
    return f"{SOURCE_PREFIX}_{task_id}"


def is_archived(task: Dict[str, Any]) -> bool:
    archived = task.get("archived")
    if isinstance(archived, str):
        return archived.strip().lower() in {"1", "true", "yes"}

    return bool(archived)


def build_task_structure(
    tasks: List[Dict[str, Any]],
    active_only: bool = True,
) -> List[Dict[str, Any]]:
    """Convert TimeCamp tasks into sync_projects.py flat task structure."""
    included_tasks: List[Dict[str, Any]] = []
    included_source_ids = set()

    for task in tasks:
        source_task_id = task.get("task_id")
        name = task.get("name")
        if source_task_id in (None, ""):
            print(f"Warning: skipping TimeCamp task without task_id: {task!r}")
            continue
        if not name:
            print(f"Warning: skipping TimeCamp task without name: {task!r}")
            continue
        if active_only and is_archived(task):
            continue

        source_task_id = str(source_task_id)
        included_tasks.append({**task, "task_id": source_task_id})
        included_source_ids.add(source_task_id)

    flattened_data: List[Dict[str, Any]] = []
    for task in included_tasks:
        source_task_id = str(task["task_id"])
        source_parent_id = task.get("parent_id")
        parent_id: Any = 0

        if source_parent_id not in (None, "", 0, "0"):
            source_parent_id = str(source_parent_id)
            if source_parent_id in included_source_ids and source_parent_id != source_task_id:
                parent_id = prefixed_task_id(source_parent_id)

        flattened_data.append(
            {
                "name": task["name"],
                "task_id": prefixed_task_id(source_task_id),
                "parent_id": parent_id,
                "timecamp_task_id": source_task_id,
            }
        )

    return flattened_data


class TimeCampFetcher:
    """Fetches TimeCamp tasks and outputs tasks.json format."""

    def __init__(
        self,
        api_token: Optional[str] = None,
        client_cls: Type[TimeCampClient] = TimeCampClient,
    ):
        api_token = api_token or os.getenv("TIMECAMP_API_TOKEN_FETCH")
        if not api_token:
            raise ValueError("TIMECAMP_API_TOKEN_FETCH must be set in .env")

        self.client = client_cls(api_token)

    def fetch_all_data(self, active_only: bool = True) -> List[Dict[str, Any]]:
        print("Fetching tasks from TimeCamp...")
        tasks = self.client.get_tasks()
        print(f"  Found {len(tasks)} tasks")

        data = build_task_structure(tasks, active_only=active_only)
        skipped = len(tasks) - len(data)
        if skipped:
            print(f"  Skipped {skipped} archived or invalid tasks")

        return data

    def save_to_json(
        self,
        data: List[Dict[str, Any]],
        filename: str = DEFAULT_OUTPUT_FILE,
    ) -> str:
        """Save data to JSON file."""
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch TimeCamp tasks into tasks.json format for TimeCamp sync."
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Output file path (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived TimeCamp tasks in the output",
    )

    return parser.parse_args()


def main():
    """Fetch TimeCamp tasks -> tasks.json."""
    load_dotenv(override=True)
    args = parse_args()

    print("Starting TimeCamp data fetch...")
    print(f"Started at: {datetime.now()}")

    fetcher = TimeCampFetcher()
    data = fetcher.fetch_all_data(active_only=not args.include_archived)
    filename = fetcher.save_to_json(data, args.output)

    root_tasks = len([item for item in data if item["parent_id"] == 0])
    child_tasks = len(data) - root_tasks

    print(f"\nData fetch completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")
    print("\nSummary:")
    print(f"  Root tasks: {root_tasks}")
    print(f"  Child tasks: {child_tasks}")
    print(f"  Total items: {len(data)}")

    if data:
        print("\nStructure preview:")
        for item in data[:20]:
            indent = "" if item["parent_id"] == 0 else "  "
            level = "[ROOT]" if item["parent_id"] == 0 else "[TASK]"
            print(
                f"  {indent}{level} {item['name']} "
                f"(ID: {item['task_id']}, Parent: {item['parent_id']})"
            )

        if len(data) > 20:
            print(f"  ... and {len(data) - 20} more items")


if __name__ == "__main__":
    main()

import argparse
import os
import sys
from typing import Any, Dict, List, Optional


TIMECAMP_TASKS_URL = "https://app.timecamp.com/third_party/api/tasks"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Archive root-level TimeCamp tasks by moving them under another task."
        ),
        epilog=(
            "Examples:\n"
            "  python3 helpers/archive.py --subtask-of 999 --dry-run\n"
            "  python3 helpers/archive.py --subtask-of 999"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--subtask-of",
        required=True,
        type=int,
        help="TimeCamp task ID that should become the parent of moved tasks.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the tasks that would be moved without updating TimeCamp.",
    )

    return parser.parse_args()


class TimeCampClient:
    def __init__(self, api_token: str):
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Missing dependency: requests. Run: pip install -r requirements.txt"
            ) from exc

        self.requests = requests
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def get_tasks(self) -> List[Dict[str, Any]]:
        response = self.requests.get(TIMECAMP_TASKS_URL, headers=self.headers)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            return list(data.values())
        if isinstance(data, list):
            return data

        raise ValueError(f"Unexpected TimeCamp tasks response: {type(data)}")

    def move_task(self, task_id: int, parent_id: int) -> Dict[str, Any]:
        payload = {
            "task_id": task_id,
            "parent_id": parent_id,
        }
        response = self.requests.put(
            TIMECAMP_TASKS_URL,
            headers=self.headers,
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def normalize_task_id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def task_name(task: Dict[str, Any]) -> str:
    return str(task.get("name") or "(unnamed task)")


def is_archived(task: Dict[str, Any]) -> bool:
    archived = task.get("archived")
    if isinstance(archived, str):
        return archived.lower() in {"1", "true", "yes"}

    return bool(archived)


def select_root_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected = []

    for task in tasks:
        parent_id = normalize_task_id(task.get("parent_id"))
        if parent_id not in (None, 0):
            continue
        if is_archived(task):
            continue

        selected.append(task)

    return selected


def filter_movable_tasks(
    tasks: List[Dict[str, Any]],
    archive_parent_id: int,
    tasks_by_id: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    movable = []

    def archive_parent_is_descendant_of(task_id: int) -> bool:
        current_parent_id = archive_parent_id
        seen = set()

        while current_parent_id not in seen:
            seen.add(current_parent_id)
            current_task = tasks_by_id.get(current_parent_id)
            if not current_task:
                return False

            current_parent_id = normalize_task_id(current_task.get("parent_id"))
            if current_parent_id is None:
                return False
            if current_parent_id == task_id:
                return True

        return False

    for task in tasks:
        task_id = normalize_task_id(task.get("task_id"))
        parent_id = normalize_task_id(task.get("parent_id"))

        if task_id is None:
            print(f"Warning: skipping task without numeric task_id: {task!r}")
            continue
        if task_id == archive_parent_id:
            print(f"Skipping archive parent itself: {task_id} {task_name(task)}")
            continue
        if parent_id == archive_parent_id:
            print(f"Skipping already archived task: {task_id} {task_name(task)}")
            continue
        if archive_parent_is_descendant_of(task_id):
            print(
                "Skipping task because the archive parent is its descendant: "
                f"{task_id} {task_name(task)}"
            )
            continue

        movable.append(task)

    return movable


def main() -> int:
    args = parse_args()

    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        load_dotenv = None

    if load_dotenv:
        load_dotenv(override=True)

    api_token = os.getenv("TIMECAMP_API_TOKEN")
    if not api_token:
        print(
            "Error: TIMECAMP_API_TOKEN is not set. "
            "If you rely on .env, run: pip install -r requirements.txt"
        )
        return 1

    try:
        client = TimeCampClient(api_token)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 1
    tasks = client.get_tasks()
    tasks_by_id = {
        task_id: task
        for task in tasks
        if (task_id := normalize_task_id(task.get("task_id"))) is not None
    }

    if args.subtask_of not in tasks_by_id:
        print(f"Error: archive parent task not found: {args.subtask_of}")
        return 1

    selected = select_root_tasks(tasks)

    selected_by_id = {
        normalize_task_id(task.get("task_id")): task
        for task in selected
        if normalize_task_id(task.get("task_id")) is not None
    }
    movable_tasks = filter_movable_tasks(
        list(selected_by_id.values()),
        archive_parent_id=args.subtask_of,
        tasks_by_id=tasks_by_id,
    )

    if not movable_tasks:
        print("No tasks to archive.")
        return 0

    action = "Would move" if args.dry_run else "Moving"
    print(f"{action} {len(movable_tasks)} task(s) under {args.subtask_of}:")

    for task in movable_tasks:
        task_id = normalize_task_id(task.get("task_id"))
        print(f"  - {task_id}: {task_name(task)}")

        if args.dry_run or task_id is None:
            continue

        client.move_task(task_id=task_id, parent_id=args.subtask_of)

    print("Dry run complete." if args.dry_run else "Archive move complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
